from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from apps.common.managers import TenantManager, TenantQuerySet
from apps.common.models import SoftDeleteMixin, TenantMixin


class Schedule(TenantMixin):
    class DayOfWeek(models.IntegerChoices):
        MONDAY = 0, "Monday"
        TUESDAY = 1, "Tuesday"
        WEDNESDAY = 2, "Wednesday"
        THURSDAY = 3, "Thursday"
        FRIDAY = 4, "Friday"
        SATURDAY = 5, "Saturday"
        SUNDAY = 6, "Sunday"

    day_of_week = models.IntegerField(choices=DayOfWeek.choices)
    start_time = models.TimeField()
    end_time = models.TimeField()
    group_name = models.CharField(max_length=100)
    trainer = models.ForeignKey(
        "trainers.Trainer",
        on_delete=models.PROTECT,
        related_name="schedules",
    )
    location = models.ForeignKey(
        "clubs.Location",
        on_delete=models.PROTECT,
        related_name="schedules",
    )
    training_type = models.ForeignKey(
        "billing.TrainingType",
        on_delete=models.PROTECT,
        related_name="schedules",
        null=True,
        blank=True,
    )
    training_group = models.ForeignKey(
        "TrainingGroup",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="schedules",
    )
    is_active = models.BooleanField(default=True)
    one_time_date = models.DateField(
        null=True, blank=True,
        help_text="If set, slot appears only on this date (not recurring weekly)",
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(end_time__gt=models.F("start_time")),
                name="att_schedule_end_after_start",
            ),
            models.UniqueConstraint(
                fields=["club", "trainer", "one_time_date", "start_time", "end_time"],
                condition=Q(one_time_date__isnull=False, is_active=True),
                name="uniq_one_time_schedule_trainer_slot",
            ),
        ]
        indexes = [
            models.Index(fields=["club", "day_of_week"]),
            models.Index(fields=["club", "one_time_date"]),
        ]

    def __str__(self):
        if self.one_time_date:
            return f"{self.group_name} - {self.one_time_date} {self.start_time}"
        return f"{self.group_name} - {self.get_day_of_week_display()} {self.start_time}"

    def clean(self):
        super().clean()
        errors = {}
        if self.training_group_id:
            training_group = self.training_group
            if self.club_id and training_group.club_id != self.club_id:
                errors["training_group"] = "Training group must belong to the same club as schedule."
            if self.one_time_date:
                errors["training_group"] = "One-time schedules cannot belong to a training group."
            if self.training_type_id and training_group.training_type_id != self.training_type_id:
                errors["training_group"] = "Training group must match the schedule training type."
            if self.location_id and training_group.location_id != self.location_id:
                errors["training_group"] = "Training group must match the schedule location."
            if self.training_type_id and self.training_type.kind != "group":
                errors["training_type"] = "Only group training schedules can belong to a training group."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if self._state.adding and self.training_group_id:
            self.group_name = self.training_group.name
        if not self._state.adding:
            previous_group_id = type(self).objects.filter(pk=self.pk).values_list(
                "training_group_id", flat=True
            ).first()
            if previous_group_id and previous_group_id != self.training_group_id:
                has_history = (
                    self.enrollments.exists()
                    or self.exceptions.exists()
                    or self.checkins.exists()
                    or self.sessions.exists()
                    or self.conversion_payments.exists()
                )
                if has_history:
                    raise ValidationError(
                        {"training_group": "A historical training-group schedule cannot be reassigned."}
                    )
        return super().save(*args, **kwargs)


class ScheduleEnrollment(TenantMixin):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        TRIAL = "trial", "Trial"
        FROZEN = "frozen", "Frozen"
        TRANSFERRED = "transferred", "Transferred"
        CANCELLED = "cancelled", "Cancelled"

    class CreatedFrom(models.TextChoices):
        MANUAL = "manual", "Manual"
        LEAD_BOOKING = "lead_booking", "Lead booking"
        GUEST_VISIT = "guest_visit", "Guest visit"
        PERSONAL_BOOKING = "personal_booking", "Personal booking"
        PERSONAL_DROP_IN = "personal_drop_in", "Personal drop-in"
        STUDENT_SELF_BOOKING = "student_self_booking", "Student self-booking"
        PAID_CONVERSION = "paid_conversion", "Paid conversion"
        GROUP_PROJECTION = "group_projection", "Group projection"
        IMPORT = "import", "Import"

    student = models.ForeignKey(
        "students.Student",
        on_delete=models.PROTECT,
        related_name="schedule_enrollments",
    )
    schedule = models.ForeignKey(
        Schedule,
        on_delete=models.PROTECT,
        related_name="enrollments",
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    starts_on = models.DateField(null=True, blank=True)
    ends_on = models.DateField(null=True, blank=True)
    trial_at = models.DateTimeField(null=True, blank=True)
    created_from = models.CharField(
        max_length=20,
        choices=CreatedFrom.choices,
        default=CreatedFrom.MANUAL,
    )
    training_group_membership = models.ForeignKey(
        "TrainingGroupMembership",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="schedule_enrollment_projections",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["club", "student", "schedule"],
                condition=Q(
                    ends_on__isnull=True,
                    status__in=[
                        "active",
                        "trial",
                        "frozen",
                    ],
                ),
                name="uniq_open_schedule_enrollment",
            ),
            models.UniqueConstraint(
                fields=["club", "student", "schedule", "starts_on", "ends_on"],
                condition=Q(
                    starts_on__isnull=False,
                    ends_on__isnull=False,
                    status__in=[
                        "active",
                        "trial",
                        "frozen",
                    ],
                ),
                name="uniq_dated_schedule_enrollment",
            ),
        ]
        indexes = [
            models.Index(fields=["club", "schedule", "status"]),
            models.Index(fields=["club", "student", "status"]),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.club_id and self.student_id and self.student.club_id != self.club_id:
            errors["student"] = "Student does not belong to enrollment club."
        if self.club_id and self.schedule_id and self.schedule.club_id != self.club_id:
            errors["schedule"] = "Schedule does not belong to enrollment club."
        if self.club_id and self.training_group_membership_id:
            membership = self.training_group_membership
            if membership.club_id != self.club_id:
                errors["training_group_membership"] = "Training group membership does not belong to enrollment club."
            is_generated_projection = self.created_from == self.CreatedFrom.GROUP_PROJECTION
            is_preserved_migration_source = (
                self.created_from
                in {
                    self.CreatedFrom.MANUAL,
                    self.CreatedFrom.PAID_CONVERSION,
                    self.CreatedFrom.IMPORT,
                }
                and membership.source == membership.Source.MIGRATION
            )
            if not (is_generated_projection or is_preserved_migration_source):
                errors["created_from"] = (
                    "Only group projections or preserved migration sources may link "
                    "to a training group membership."
                )
            if self.schedule_id and self.schedule.training_group_id != membership.training_group_id:
                errors["training_group_membership"] = "Membership must match the schedule training group."
        elif self.created_from == self.CreatedFrom.GROUP_PROJECTION:
            errors["training_group_membership"] = "Group-projection enrollments require a training group membership."
        if self.starts_on and self.ends_on and self.ends_on < self.starts_on:
            errors["ends_on"] = "Enrollment end date cannot be before start date."
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"{self.student} -> {self.schedule.group_name} ({self.status})"


class TrainingGroup(TenantMixin):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        ACTIVE = "active", "Active"
        ARCHIVED = "archived", "Archived"

    name = models.CharField(max_length=200)
    training_type = models.ForeignKey(
        "billing.TrainingType",
        on_delete=models.PROTECT,
        related_name="training_groups",
    )
    location = models.ForeignKey(
        "clubs.Location",
        on_delete=models.PROTECT,
        related_name="training_groups",
    )
    responsible_trainer = models.ForeignKey(
        "trainers.Trainer",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="responsible_training_groups",
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="created_training_groups",
    )

    class Meta:
        indexes = [
            models.Index(fields=["club", "status"], name="att_group_club_status_idx"),
            models.Index(fields=["club", "training_type", "location"], name="att_group_scope_idx"),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.club_id and self.training_type_id and self.training_type.club_id != self.club_id:
            errors["training_type"] = "Training type must belong to the same club as training group."
        if self.club_id and self.location_id and self.location.club_id != self.club_id:
            errors["location"] = "Location must belong to the same club as training group."
        if self.club_id and self.responsible_trainer_id:
            if self.responsible_trainer.club_id != self.club_id:
                errors["responsible_trainer"] = "Responsible trainer must belong to the same club as training group."
            elif self.status == self.Status.ACTIVE and not self.responsible_trainer.is_active:
                errors["responsible_trainer"] = "An active training group requires an active responsible trainer."
        if self.training_type_id and self.training_type.kind != "group":
            errors["training_type"] = "Training groups require a group training type."
        if self.status == self.Status.ACTIVE and not self.responsible_trainer_id:
            errors["responsible_trainer"] = "An active training group requires a responsible trainer."
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return self.name


class TrainingGroupMembership(TenantMixin):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        FROZEN = "frozen", "Frozen"
        TRANSFERRED = "transferred", "Transferred"
        CANCELLED = "cancelled", "Cancelled"

    class Source(models.TextChoices):
        MANUAL = "manual", "Manual"
        PAID_CONVERSION = "paid_conversion", "Paid conversion"
        IMPORT = "import", "Import"
        MIGRATION = "migration", "Migration"
        TRANSFER = "transfer", "Transfer"

    class Authority(models.TextChoices):
        INDEPENDENT = "independent", "Independent"
        PAYMENT_OWNED = "payment_owned", "Payment owned"

    student = models.ForeignKey(
        "students.Student",
        on_delete=models.PROTECT,
        related_name="training_group_memberships",
    )
    training_group = models.ForeignKey(
        TrainingGroup,
        on_delete=models.PROTECT,
        related_name="memberships",
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    starts_on = models.DateField()
    ends_on = models.DateField(null=True, blank=True)
    source = models.CharField(max_length=20, choices=Source.choices)
    authority = models.CharField(
        max_length=20,
        choices=Authority.choices,
        default=Authority.INDEPENDENT,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="created_training_group_memberships",
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(ends_on__isnull=True) | Q(ends_on__gte=models.F("starts_on")),
                name="att_group_membership_end_after_start",
            ),
            models.UniqueConstraint(
                fields=["club", "student", "training_group"],
                condition=Q(ends_on__isnull=True, status__in=["active", "frozen"]),
                name="uniq_open_training_group_membership",
            ),
        ]
        indexes = [
            models.Index(fields=["club", "training_group", "status"], name="att_groupmem_group_status_idx"),
            models.Index(fields=["club", "student", "status"], name="att_groupmem_student_idx"),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.club_id and self.student_id and self.student.club_id != self.club_id:
            errors["student"] = "Student must belong to the same club as training group membership."
        if self.club_id and self.training_group_id:
            training_group = self.training_group
            if training_group.club_id != self.club_id:
                errors["training_group"] = "Training group must belong to the same club as membership."
            elif (
                self.status in {self.Status.ACTIVE, self.Status.FROZEN}
                and training_group.status != TrainingGroup.Status.ACTIVE
            ):
                errors["training_group"] = "Open memberships require an active training group."
        if self.starts_on and self.ends_on and self.ends_on < self.starts_on:
            errors["ends_on"] = "Membership end date cannot be before start date."
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"{self.student_id} -> {self.training_group_id} ({self.status})"


_TRAINING_GROUP_ROLLOUT_TRANSITION_TOKEN = object()


class _TrainingGroupRolloutStateQuerySet(TenantQuerySet):
    _transition_fields = frozenset({"mode", "reconciling_from_mode"})

    def update(self, **kwargs):
        if self._transition_fields.intersection(kwargs):
            raise ValidationError("Rollout state transitions are service-owned.")
        return super().update(**kwargs)

    def bulk_create(self, objs, **kwargs):
        objs = list(objs)
        if any(obj.mode != "off" or obj.reconciling_from_mode for obj in objs):
            raise ValidationError("New rollout states must start off.")
        return super().bulk_create(objs, **kwargs)

    def _update_for_transition(self, *, transition_token, **kwargs):
        if transition_token is not _TRAINING_GROUP_ROLLOUT_TRANSITION_TOKEN:
            raise ValidationError("Rollout state transitions are service-owned.")
        if not self._transition_fields.intersection(kwargs):
            raise ValidationError("Rollout transition must update transition state.")
        return super().update(**kwargs)


class _TrainingGroupRolloutStateManager(TenantManager):
    def get_queryset(self):
        return _TrainingGroupRolloutStateQuerySet(self.model, using=self._db)

    def unscoped(self):
        return self.get_queryset()


class TrainingGroupRolloutState(TenantMixin):
    class Mode(models.TextChoices):
        OFF = "off", "Off"
        RECONCILING = "reconciling", "Reconciling"
        SHADOW = "shadow", "Shadow"
        ACTIVE = "active", "Active"
        CONTAINMENT = "containment", "Containment"

    mode = models.CharField(max_length=20, choices=Mode.choices, default=Mode.OFF)
    reconciling_from_mode = models.CharField(max_length=20, choices=Mode.choices, blank=True, default="")
    objects = _TrainingGroupRolloutStateManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["club"], name="uniq_training_group_rollout_per_club"),
        ]

    def save(self, *args, **kwargs):
        if self._state.adding:
            if self.mode != self.Mode.OFF:
                raise ValidationError({"mode": "New rollout state must start off."})
        else:
            previous = type(self).objects.filter(pk=self.pk).values("mode", "reconciling_from_mode").first()
            if previous and (
                previous["mode"] != self.mode
                or previous["reconciling_from_mode"] != self.reconciling_from_mode
            ):
                raise ValidationError({"mode": "Rollout state transitions are service-owned."})
        return super().save(*args, **kwargs)


class _AppendOnlyTrainingGroupEventQuerySet(TenantQuerySet):
    def update(self, **kwargs):
        raise ValidationError("Training group events are append-only.")

    def delete(self):
        raise ValidationError("Training group events are append-only.")


class _AppendOnlyTrainingGroupEventManager(TenantManager):
    def get_queryset(self):
        return _AppendOnlyTrainingGroupEventQuerySet(self.model, using=self._db)


class _AppendOnlyTrainingGroupEvent(TenantMixin):
    """Durable evidence records are created once and never rewritten."""

    idempotency_key = models.CharField(max_length=120)
    objects = _AppendOnlyTrainingGroupEventManager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Training group events are append-only.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Training group events are append-only.")


class TrainingGroupRolloutEvent(_AppendOnlyTrainingGroupEvent):
    previous_mode = models.CharField(max_length=20, choices=TrainingGroupRolloutState.Mode.choices)
    new_mode = models.CharField(max_length=20, choices=TrainingGroupRolloutState.Mode.choices)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="training_group_rollout_events",
    )
    rationale = models.CharField(max_length=500)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["club", "idempotency_key"],
                name="uniq_training_group_rollout_event_key",
            ),
        ]
        indexes = [models.Index(fields=["club", "created_at"], name="att_rollout_event_club_idx")]


class TrainingGroupMappingEvent(_AppendOnlyTrainingGroupEvent):
    batch_id = models.UUIDField()
    training_group = models.ForeignKey(
        TrainingGroup,
        on_delete=models.PROTECT,
        related_name="mapping_events",
    )
    schedule = models.ForeignKey(
        Schedule,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="training_group_mapping_events",
    )
    action = models.CharField(max_length=40)
    previous_group_snapshot = models.JSONField(default=dict, blank=True)
    new_group_snapshot = models.JSONField(default=dict, blank=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="training_group_mapping_events",
    )
    rationale = models.CharField(max_length=500)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["club", "idempotency_key"],
                name="uniq_training_group_mapping_event_key",
            ),
        ]
        indexes = [
            models.Index(fields=["club", "batch_id"], name="att_mapping_event_batch_idx"),
            models.Index(fields=["club", "training_group", "created_at"], name="att_mapping_event_group_idx"),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.club_id and self.training_group_id and self.training_group.club_id != self.club_id:
            errors["training_group"] = "Training group must belong to the mapping event club."
        if self.club_id and self.schedule_id and self.schedule.club_id != self.club_id:
            errors["schedule"] = "Schedule must belong to the mapping event club."
        if self.schedule_id and self.training_group_id and self.schedule.training_group_id:
            if self.schedule.training_group_id != self.training_group_id:
                errors["schedule"] = "Mapped schedule must match the training group."
        if errors:
            raise ValidationError(errors)


class TrainingGroupMembershipEvent(_AppendOnlyTrainingGroupEvent):
    membership = models.ForeignKey(
        TrainingGroupMembership,
        on_delete=models.PROTECT,
        related_name="events",
    )
    action = models.CharField(max_length=40)
    effective_date = models.DateField()
    previous_state_snapshot = models.JSONField(default=dict, blank=True)
    new_state_snapshot = models.JSONField(default=dict, blank=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="training_group_membership_events",
    )
    rationale = models.CharField(max_length=500)
    source_enrollment_id = models.PositiveBigIntegerField(null=True, blank=True)
    source_payment_id = models.PositiveBigIntegerField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["club", "idempotency_key"],
                name="uniq_training_group_membership_event_key",
            ),
        ]
        indexes = [
            models.Index(fields=["club", "membership", "created_at"], name="att_member_event_idx"),
        ]

    def clean(self):
        super().clean()
        if self.club_id and self.membership_id and self.membership.club_id != self.club_id:
            raise ValidationError({"membership": "Membership must belong to the event club."})


class ScheduleBookingEvent(TenantMixin):
    class EventType(models.TextChoices):
        GUEST_VISIT_BOOKED = "guest_visit_booked", "Guest visit booked"
        PERSONAL_SESSION_BOOKED = "personal_session_booked", "Personal session booked"
        GUEST_VISIT_CANCELLED = "guest_visit_cancelled", "Guest visit cancelled"
        PERSONAL_SESSION_CANCELLED = "personal_session_cancelled", "Personal session cancelled"
        PERSONAL_SESSION_RESCHEDULED = "personal_session_rescheduled", "Personal session rescheduled"
        PERSONAL_DROP_IN_CANCELLED = "personal_drop_in_cancelled", "Personal drop-in cancelled"
        PERSONAL_DROP_IN_NO_SHOW = "personal_drop_in_no_show", "Personal drop-in no-show"

    class Origin(models.TextChoices):
        PLANNED_SESSION_ACTION = "planned_session_action", "Planned session action"
        WALK_IN_CHECKIN = "walk_in_checkin", "Walk-in check-in"
        STUDENT_SELF_BOOKING = "student_self_booking", "Student self-booking"
        PARENT_SELF_BOOKING = "parent_self_booking", "Parent self-booking"

    enrollment = models.ForeignKey(
        ScheduleEnrollment,
        on_delete=models.PROTECT,
        related_name="booking_events",
    )
    schedule = models.ForeignKey(
        Schedule,
        on_delete=models.PROTECT,
        related_name="booking_events",
    )
    student = models.ForeignKey(
        "students.Student",
        on_delete=models.PROTECT,
        related_name="schedule_booking_events",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="schedule_booking_events",
    )
    event_type = models.CharField(max_length=32, choices=EventType.choices, db_index=True)
    origin = models.CharField(max_length=32, choices=Origin.choices)
    effective_date = models.DateField()
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["club", "schedule", "effective_date"]),
            models.Index(fields=["club", "student", "created_at"]),
            models.Index(fields=["club", "event_type", "created_at"]),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.club_id and self.enrollment_id and self.enrollment.club_id != self.club_id:
            errors["enrollment"] = "Enrollment does not belong to booking event club."
        if self.club_id and self.schedule_id and self.schedule.club_id != self.club_id:
            errors["schedule"] = "Schedule does not belong to booking event club."
        if self.club_id and self.student_id and self.student.club_id != self.club_id:
            errors["student"] = "Student does not belong to booking event club."
        if self.enrollment_id and self.schedule_id and self.enrollment.schedule_id != self.schedule_id:
            errors["schedule"] = "Schedule does not match enrollment."
        if self.enrollment_id and self.student_id and self.enrollment.student_id != self.student_id:
            errors["student"] = "Student does not match enrollment."
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"{self.event_type}:{self.schedule_id}:{self.student_id}:{self.effective_date}"


class PersonalStaffIntentCommandQuerySet(TenantQuerySet):
    """Prevent bulk rewrites of durable cross-family command evidence."""

    def update(self, **kwargs):
        immutable_fields = {
            "club",
            "club_id",
            "command_key",
            "command_fingerprint",
            "payment_method",
            "command_shape",
            "booking_id_snapshot",
            "reservation_id_snapshot",
            "enrollment_id_snapshot",
            "payment_link_id_snapshot",
            "result_bound_at",
        }
        if immutable_fields & set(kwargs):
            raise ValidationError("Personal staff command evidence is immutable.")
        return super().update(**kwargs)


class PersonalStaffIntentCommandManager(TenantManager):
    def get_queryset(self):
        return PersonalStaffIntentCommandQuerySet(self.model, using=self._db)

    def unscoped(self):
        return self.get_queryset()


class PersonalStaffIntentCommand(TenantMixin):
    """One durable identity claim for every flag-on staff personal command.

    Booking events, drop-in bookings, reservations and Payments intentionally
    belong to different lifecycle owners.  None of their local unique keys can
    therefore prove that a client command was not reused for another method or
    target.  This small command record is the cross-family authority; its
    fingerprint is calculated from the complete server-facing command shape.
    """

    command_key = models.CharField(max_length=120)
    command_fingerprint = models.CharField(max_length=64)
    payment_method = models.CharField(max_length=20)
    command_shape = models.JSONField(default=dict)
    booking_id_snapshot = models.PositiveBigIntegerField(null=True, blank=True)
    reservation_id_snapshot = models.PositiveBigIntegerField(null=True, blank=True)
    enrollment_id_snapshot = models.PositiveBigIntegerField(null=True, blank=True)
    payment_link_id_snapshot = models.PositiveBigIntegerField(null=True, blank=True)
    result_bound_at = models.DateTimeField(null=True, blank=True)

    objects = PersonalStaffIntentCommandManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["club", "command_key"],
                name="uniq_personal_staff_intent_command_key",
            ),
        ]
        indexes = [
            models.Index(fields=["club", "created_at"], name="att_personal_cmd_created_idx"),
        ]

    def __str__(self):
        return f"personal-command:{self.club_id}:{self.command_key}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            previous = type(self).objects.filter(pk=self.pk).values(
                "club_id",
                "command_key",
                "command_fingerprint",
                "payment_method",
                "command_shape",
                "booking_id_snapshot",
                "reservation_id_snapshot",
                "enrollment_id_snapshot",
                "payment_link_id_snapshot",
                "result_bound_at",
            ).first()
            if previous:
                immutable_identity_fields = (
                    "club_id",
                    "command_key",
                    "command_fingerprint",
                    "payment_method",
                    "command_shape",
                )
                for field_name in immutable_identity_fields:
                    if previous[field_name] != getattr(self, field_name):
                        raise ValidationError({field_name: "Personal staff command evidence is immutable."})
                snapshot_fields = (
                    "booking_id_snapshot",
                    "reservation_id_snapshot",
                    "enrollment_id_snapshot",
                    "payment_link_id_snapshot",
                    "result_bound_at",
                )
                for field_name in snapshot_fields:
                    old_value = previous[field_name]
                    new_value = getattr(self, field_name)
                    if old_value is not None and old_value != new_value:
                        raise ValidationError({field_name: "Personal staff command result is immutable once bound."})
                if previous["result_bound_at"] is not None and any(
                    previous[field_name] != getattr(self, field_name)
                    for field_name in snapshot_fields
                ):
                    raise ValidationError("Personal staff command result is immutable once bound.")
        return super().save(*args, **kwargs)


class PersonalSelfServiceCommandQuerySet(TenantQuerySet):
    """Keep self-service cross-family command evidence append-only."""

    def update(self, **kwargs):
        immutable_fields = {
            "club",
            "club_id",
            "command_key",
            "command_fingerprint",
            "actor",
            "actor_id",
            "source",
            "student",
            "student_id",
            "availability_slot",
            "availability_slot_id",
            "action",
            "offer_digest",
            "enrollment_id_snapshot",
            "reservation_id_snapshot",
            "enrollment",
            "enrollment_id",
            "entitlement_subscription",
            "entitlement_subscription_id",
            "entitlement_component",
            "entitlement_component_id",
            "result_bound_at",
        }
        if immutable_fields & set(kwargs):
            raise ValidationError("Personal self-service command evidence is immutable.")
        return super().update(**kwargs)


class PersonalSelfServiceCommandManager(TenantManager):
    def get_queryset(self):
        return PersonalSelfServiceCommandQuerySet(self.model, using=self._db)

    def unscoped(self):
        return self.get_queryset()


class PersonalSelfServiceCommand(TenantMixin):
    """One actor-derived command identity across entitlement and SBP paths.

    The command intentionally stores only immutable input/result pointers.  The
    booking, reservation, payment, subscription, and bank order remain owned
    by their existing lifecycle models.
    """

    class Source(models.TextChoices):
        STUDENT = "student", "Student"
        PARENT = "parent", "Parent"

    class Action(models.TextChoices):
        BOOK = "book", "Book from entitlement"
        PAY = "pay", "Pay by SBP"

    command_key = models.CharField(max_length=120)
    command_fingerprint = models.CharField(max_length=64)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="personal_self_service_commands",
    )
    source = models.CharField(max_length=20, choices=Source.choices)
    student = models.ForeignKey(
        "students.Student",
        on_delete=models.PROTECT,
        related_name="personal_self_service_commands",
    )
    availability_slot = models.ForeignKey(
        "PersonalAvailabilitySlot",
        on_delete=models.PROTECT,
        related_name="self_service_commands",
    )
    enrollment = models.OneToOneField(
        "ScheduleEnrollment",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="self_service_personal_command",
    )
    entitlement_subscription = models.ForeignKey(
        "billing.Subscription",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="personal_self_service_commands",
    )
    entitlement_component = models.ForeignKey(
        "billing.SubscriptionComponent",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="personal_self_service_commands",
    )
    action = models.CharField(max_length=20, choices=Action.choices)
    # Only an opaque displayed SBP offer digest is retained.  It is evidence
    # of what the caller saw; locked offer resolution remains authoritative.
    offer_digest = models.CharField(max_length=64, blank=True, default="")
    enrollment_id_snapshot = models.PositiveBigIntegerField(null=True, blank=True)
    reservation_id_snapshot = models.PositiveBigIntegerField(null=True, blank=True)
    result_bound_at = models.DateTimeField(null=True, blank=True)

    objects = PersonalSelfServiceCommandManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["club", "command_key"],
                name="uniq_personal_self_service_command_key",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        result_bound_at__isnull=True,
                        enrollment_id_snapshot__isnull=True,
                        reservation_id_snapshot__isnull=True,
                        enrollment__isnull=True,
                        entitlement_subscription__isnull=True,
                        entitlement_component__isnull=True,
                    )
                    | models.Q(
                        result_bound_at__isnull=False,
                        enrollment_id_snapshot__isnull=False,
                        reservation_id_snapshot__isnull=True,
                        enrollment__isnull=False,
                        entitlement_subscription__isnull=False,
                    )
                    | models.Q(
                        result_bound_at__isnull=False,
                        enrollment_id_snapshot__isnull=True,
                        reservation_id_snapshot__isnull=False,
                        enrollment__isnull=True,
                        entitlement_subscription__isnull=True,
                        entitlement_component__isnull=True,
                    )
                ),
                name="personal_self_service_command_binding",
            ),
        ]
        indexes = [
            models.Index(
                fields=["club", "student", "source", "created_at"],
                name="att_self_service_cmd_actor_idx",
            ),
        ]

    def __str__(self):
        return f"personal-self-service:{self.club_id}:{self.command_key}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            previous = type(self).objects.filter(pk=self.pk).values(
                "club_id",
                "command_key",
                "command_fingerprint",
                "actor_id",
                "source",
                "student_id",
                "availability_slot_id",
                "action",
                "offer_digest",
                "enrollment_id_snapshot",
                "reservation_id_snapshot",
                "enrollment_id",
                "entitlement_subscription_id",
                "entitlement_component_id",
                "result_bound_at",
            ).first()
            if previous is not None:
                immutable_fields = (
                    "club_id",
                    "command_key",
                    "command_fingerprint",
                    "actor_id",
                    "source",
                    "student_id",
                    "availability_slot_id",
                    "action",
                    "offer_digest",
                )
                for field_name in immutable_fields:
                    if previous[field_name] != getattr(self, field_name):
                        raise ValidationError("Personal self-service command evidence is immutable.")
                if previous["result_bound_at"] is not None and (
                    previous["enrollment_id_snapshot"] != self.enrollment_id_snapshot
                    or previous["reservation_id_snapshot"] != self.reservation_id_snapshot
                    or previous["enrollment_id"] != self.enrollment_id
                    or previous["entitlement_subscription_id"] != self.entitlement_subscription_id
                    or previous["entitlement_component_id"] != self.entitlement_component_id
                    or previous["result_bound_at"] != self.result_bound_at
                ):
                    raise ValidationError("Personal self-service command result is immutable once bound.")
        return super().save(*args, **kwargs)


class PersonalAvailabilitySlot(TenantMixin):
    class Status(models.TextChoices):
        PUBLISHED = "published", "Published"
        HELD = "held", "Held"
        BOOKED = "booked", "Booked"
        BLOCKED = "blocked", "Blocked"
        CANCELLED = "cancelled", "Cancelled"

    trainer = models.ForeignKey(
        "trainers.Trainer",
        on_delete=models.PROTECT,
        related_name="personal_availability_slots",
    )
    location = models.ForeignKey(
        "clubs.Location",
        on_delete=models.PROTECT,
        related_name="personal_availability_slots",
    )
    training_type = models.ForeignKey(
        "billing.TrainingType",
        on_delete=models.PROTECT,
        related_name="personal_availability_slots",
    )
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PUBLISHED,
    )
    block_reason = models.CharField(max_length=255, blank=True, default="")
    booked_enrollment = models.ForeignKey(
        ScheduleEnrollment,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="personal_availability_slots",
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(ends_at__gt=models.F("starts_at")),
                name="attendance_personal_slot_ends_after_start",
            ),
            models.UniqueConstraint(
                fields=["club", "trainer", "starts_at", "ends_at"],
                condition=Q(status__in=["published", "held", "booked", "blocked"]),
                name="uniq_active_personal_availability_trainer_slot",
            ),
        ]
        indexes = [
            models.Index(fields=["club", "status", "starts_at"]),
            models.Index(fields=["club", "trainer", "starts_at"]),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.club_id and self.trainer_id and self.trainer.club_id != self.club_id:
            errors["trainer"] = "Trainer does not belong to personal slot club."
        if self.club_id and self.location_id and self.location.club_id != self.club_id:
            errors["location"] = "Location does not belong to personal slot club."
        if self.club_id and self.training_type_id and self.training_type.club_id != self.club_id:
            errors["training_type"] = "Training type does not belong to personal slot club."
        if self.starts_at and self.ends_at:
            if self.ends_at <= self.starts_at:
                errors["ends_at"] = "Personal slot end time must be after start time."
            if self.starts_at.date() != self.ends_at.date():
                errors["ends_at"] = "Personal slot must start and end on the same date."
        if self.club_id and self.booked_enrollment_id and self.booked_enrollment.club_id != self.club_id:
            errors["booked_enrollment"] = "Booked enrollment does not belong to personal slot club."
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"{self.trainer_id}:{self.starts_at}-{self.ends_at}:{self.status}"


class PersonalBookingPaymentReservation(TenantMixin):
    class Status(models.TextChoices):
        PENDING_PAYMENT = "pending_payment", "Pending payment"
        BOOKED = "booked", "Booked"
        CANCELLED = "cancelled", "Cancelled"
        EXPIRED = "expired", "Expired"
        MANUAL_REVIEW = "manual_review", "Manual review"

    student = models.ForeignKey(
        "students.Student",
        on_delete=models.PROTECT,
        related_name="personal_payment_reservations",
    )
    trainer = models.ForeignKey(
        "trainers.Trainer",
        on_delete=models.PROTECT,
        related_name="personal_payment_reservations",
    )
    location = models.ForeignKey(
        "clubs.Location",
        on_delete=models.PROTECT,
        related_name="personal_payment_reservations",
    )
    training_type = models.ForeignKey(
        "billing.TrainingType",
        on_delete=models.PROTECT,
        related_name="personal_payment_reservations",
    )
    tariff = models.ForeignKey(
        "billing.Tariff",
        on_delete=models.PROTECT,
        related_name="personal_payment_reservations",
    )
    availability_slot = models.ForeignKey(
        PersonalAvailabilitySlot,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="personal_payment_reservations",
    )
    payment = models.OneToOneField(
        "billing.Payment",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="personal_payment_reservation",
    )
    bank_payment_order = models.OneToOneField(
        "billing.BankPaymentOrder",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="personal_payment_reservation",
    )
    subscription = models.OneToOneField(
        "billing.Subscription",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="personal_payment_reservation",
    )
    schedule = models.OneToOneField(
        Schedule,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="personal_payment_reservation",
    )
    enrollment = models.OneToOneField(
        ScheduleEnrollment,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="personal_payment_reservation",
    )
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    status = models.CharField(
        max_length=30,
        choices=Status.choices,
        default=Status.PENDING_PAYMENT,
    )
    expires_at = models.DateTimeField()
    idempotency_key = models.CharField(max_length=120, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_personal_payment_reservations",
    )
    last_error_code = models.CharField(max_length=120, blank=True, default="")
    last_error_message = models.TextField(blank=True, default="")

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(ends_at__gt=models.F("starts_at")),
                name="attendance_personal_payment_reservation_ends_after_start",
            ),
            models.UniqueConstraint(
                fields=["club", "trainer", "starts_at", "ends_at"],
                condition=Q(status__in=["pending_payment", "booked", "manual_review"]),
                name="uniq_active_personal_payment_reservation_slot",
            ),
            models.UniqueConstraint(
                fields=["club", "idempotency_key"],
                condition=~Q(idempotency_key=""),
                name="uniq_personal_payment_reservation_idempotency",
            ),
        ]
        indexes = [
            models.Index(fields=["club", "status", "expires_at"], name="attendance_ppr_status_idx"),
            models.Index(fields=["club", "trainer", "starts_at"], name="attendance_ppr_trainer_idx"),
            models.Index(fields=["club", "student", "created_at"], name="attendance_ppr_student_idx"),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.club_id and self.student_id and self.student.club_id != self.club_id:
            errors["student"] = "Student does not belong to personal payment reservation club."
        if self.club_id and self.trainer_id and self.trainer.club_id != self.club_id:
            errors["trainer"] = "Trainer does not belong to personal payment reservation club."
        if self.club_id and self.location_id and self.location.club_id != self.club_id:
            errors["location"] = "Location does not belong to personal payment reservation club."
        if self.club_id and self.training_type_id and self.training_type.club_id != self.club_id:
            errors["training_type"] = "Training type does not belong to personal payment reservation club."
        if self.club_id and self.tariff_id and self.tariff.club_id != self.club_id:
            errors["tariff"] = "Tariff does not belong to personal payment reservation club."
        if self.club_id and self.availability_slot_id and self.availability_slot.club_id != self.club_id:
            errors["availability_slot"] = "Availability slot does not belong to personal payment reservation club."
        if self.club_id and self.payment_id and self.payment.club_id != self.club_id:
            errors["payment"] = "Payment does not belong to personal payment reservation club."
        if self.club_id and self.bank_payment_order_id and self.bank_payment_order.club_id != self.club_id:
            errors["bank_payment_order"] = "Bank order does not belong to personal payment reservation club."
        if self.club_id and self.subscription_id and self.subscription.club_id != self.club_id:
            errors["subscription"] = "Subscription does not belong to personal payment reservation club."
        if self.club_id and self.schedule_id and self.schedule.club_id != self.club_id:
            errors["schedule"] = "Schedule does not belong to personal payment reservation club."
        if self.club_id and self.enrollment_id and self.enrollment.club_id != self.club_id:
            errors["enrollment"] = "Enrollment does not belong to personal payment reservation club."
        if self.starts_at and self.ends_at:
            if self.ends_at <= self.starts_at:
                errors["ends_at"] = "Personal payment reservation end time must be after start time."
            if self.starts_at.date() != self.ends_at.date():
                errors["ends_at"] = "Personal payment reservation must start and end on the same date."
        if self.availability_slot_id:
            slot = self.availability_slot
            if self.trainer_id and slot.trainer_id != self.trainer_id:
                errors["availability_slot"] = "Availability slot trainer does not match reservation trainer."
            if self.location_id and slot.location_id != self.location_id:
                errors["availability_slot"] = "Availability slot location does not match reservation location."
            if self.training_type_id and slot.training_type_id != self.training_type_id:
                errors["availability_slot"] = (
                    "Availability slot training type does not match reservation training type."
                )
            if self.starts_at and slot.starts_at != self.starts_at:
                errors["availability_slot"] = "Availability slot start does not match reservation start."
            if self.ends_at and slot.ends_at != self.ends_at:
                errors["availability_slot"] = "Availability slot end does not match reservation end."
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"{self.student_id}:{self.starts_at}-{self.ends_at}:{self.status}"


class PersonalDropInBooking(TenantMixin):
    """Immutable financial intent for one staff-created pay-at-club session."""

    class State(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        ATTENDED = "attended", "Attended"
        CANCELLED = "cancelled", "Cancelled"
        NO_SHOW = "no_show", "No show"

    enrollment = models.OneToOneField(
        ScheduleEnrollment,
        on_delete=models.PROTECT,
        related_name="personal_drop_in_booking",
    )
    tariff = models.ForeignKey(
        "billing.Tariff",
        on_delete=models.PROTECT,
        related_name="personal_drop_in_bookings",
    )
    tariff_name_snapshot = models.CharField(max_length=200)
    price_snapshot = models.DecimalField(max_digits=10, decimal_places=2)
    state = models.CharField(max_length=20, choices=State.choices, default=State.SCHEDULED)
    checkin = models.OneToOneField(
        "attendance.Checkin",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="personal_drop_in_booking",
    )
    debt = models.OneToOneField(
        "billing.Debt",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="personal_drop_in_booking",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_personal_drop_in_bookings",
    )
    idempotency_key = models.CharField(max_length=120)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(price_snapshot__gt=0),
                name="attendance_dropin_price_positive",
            ),
            models.CheckConstraint(
                condition=(
                    Q(state="attended", checkin__isnull=False)
                    | ~Q(state="attended")
                ),
                name="attendance_dropin_attended_checkin",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(state__in=["scheduled", "cancelled", "no_show"])
                    | (Q(checkin__isnull=True) & Q(debt__isnull=True))
                ),
                name="attendance_dropin_terminal_no_finance",
            ),
            models.UniqueConstraint(
                fields=["club", "idempotency_key"],
                name="uniq_personal_dropin_booking_idempotency",
            ),
        ]
        indexes = [
            models.Index(fields=["club", "state", "created_at"]),
            models.Index(fields=["club", "tariff", "state"]),
        ]

    def clean(self):
        super().clean()
        errors = {}
        for field_name in ("enrollment", "tariff", "checkin", "debt"):
            value = getattr(self, field_name, None)
            if self.club_id and value is not None and value.club_id != self.club_id:
                errors[field_name] = "Object must belong to the same club as booking."
        if self.enrollment_id:
            enrollment = self.enrollment
            if enrollment.created_from != ScheduleEnrollment.CreatedFrom.PERSONAL_DROP_IN:
                errors["enrollment"] = "Drop-in booking requires a personal drop-in enrollment."
            if enrollment.starts_on != enrollment.ends_on:
                errors["enrollment"] = "Drop-in booking requires a one-day enrollment."
        if self.state == self.State.ATTENDED and not self.checkin_id:
            errors["checkin"] = "Attended drop-in booking requires a check-in."
        if self.state in {self.State.SCHEDULED, self.State.CANCELLED, self.State.NO_SHOW} and (
            self.checkin_id or self.debt_id
        ):
            errors["state"] = "Unattended or terminal booking cannot retain check-in or debt links."
        if self.debt_id and self.checkin_id and self.debt.checkin_id != self.checkin_id:
            errors["debt"] = "Drop-in debt must belong to booking check-in."
        if self.debt_id and self.enrollment_id and self.debt.student_id != self.enrollment.student_id:
            errors["debt"] = "Drop-in debt must belong to booking student."
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"Drop-in #{self.id}: enrollment {self.enrollment_id} ({self.state})"


class PersonalServiceTermsSnapshotQuerySet(TenantQuerySet):
    def update(self, **kwargs):
        raise ValidationError("Personal service terms snapshots are append-only.")

    def delete(self):
        raise ValidationError("Personal service terms snapshots are append-only.")


class PersonalServiceTermsSnapshotManager(TenantManager):
    def get_queryset(self):
        return PersonalServiceTermsSnapshotQuerySet(self.model, using=self._db)

    def unscoped(self):
        # Immutable evidence has no update/delete escape hatch.  Cross-tenant
        # reporting may read it, but it may never mutate it in bulk.
        return self.get_queryset()


COMPLETE_PERSONAL_TERMS_VERSION_VALUES = frozenset({"complete_v1", "complete_v2"})


class PersonalServiceTermsSnapshot(TenantMixin):
    """Append-only evidence of the exact one-session personal offer accepted."""

    class TermsVersion(models.TextChoices):
        COMPLETE_V1 = "complete_v1", "Complete v1"
        COMPLETE_V2 = "complete_v2", "Complete v2"
        LEGACY_PARTIAL = "legacy_partial", "Legacy partial"

    booking = models.OneToOneField(
        PersonalDropInBooking,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="terms_snapshot",
    )
    reservation = models.OneToOneField(
        PersonalBookingPaymentReservation,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="terms_snapshot",
    )
    terms_version = models.CharField(max_length=20, choices=TermsVersion.choices)
    tariff_id_snapshot = models.PositiveBigIntegerField(null=True, blank=True)
    tariff_name_snapshot = models.CharField(max_length=200, blank=True, default="")
    training_type_id_snapshot = models.PositiveBigIntegerField(null=True, blank=True)
    training_type_name_snapshot = models.CharField(max_length=100, blank=True, default="")
    base_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    discount_id_snapshot = models.PositiveBigIntegerField(null=True, blank=True)
    discount_name_snapshot = models.CharField(max_length=200, blank=True, default="")
    discount_type_snapshot = models.CharField(max_length=20, blank=True, default="")
    discount_value_snapshot = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    payable_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, default="RUB")
    duration_days = models.PositiveIntegerField(null=True, blank=True)
    scope = models.CharField(max_length=20, blank=True, default="")
    location_id_snapshot = models.PositiveBigIntegerField(null=True, blank=True)
    location_name_snapshot = models.CharField(max_length=200, blank=True, default="")
    component_name_snapshot = models.CharField(max_length=200, blank=True, default="")
    component_training_type_id_snapshot = models.PositiveBigIntegerField(null=True, blank=True)
    component_training_type_name_snapshot = models.CharField(max_length=100, blank=True, default="")
    component_entitlement_kind = models.CharField(max_length=20, blank=True, default="")
    component_credits_total = models.PositiveIntegerField(null=True, blank=True)
    component_scope = models.CharField(max_length=20, blank=True, default="")
    component_location_id_snapshot = models.PositiveBigIntegerField(null=True, blank=True)
    component_location_name_snapshot = models.CharField(max_length=200, blank=True, default="")
    tariff_trainer_payout_policy = models.CharField(max_length=20, blank=True, default="")
    component_trainer_payout_policy = models.CharField(max_length=20, blank=True, default="")
    component_paid_amount_basis = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    component_unit_amount_basis = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    objects = PersonalServiceTermsSnapshotManager()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(booking__isnull=False, reservation__isnull=True)
                    | Q(booking__isnull=True, reservation__isnull=False)
                ),
                name="attendance_personal_terms_one_target",
            ),
            models.CheckConstraint(
                condition=Q(currency="RUB"),
                name="attendance_personal_terms_rub_only",
            ),
            models.CheckConstraint(
                condition=(
                    Q(terms_version="complete_v2")
                    | Q(discount_amount=0)
                ),
                name="attendance_personal_terms_legacy_zero_discount",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(terms_version="complete_v2")
                    | (
                        Q(base_amount__gt=0)
                        & Q(discount_amount__gte=0)
                        & Q(payable_amount__gt=0)
                        & Q(base_amount=models.F("discount_amount") + models.F("payable_amount"))
                        & (
                            (
                                Q(discount_id_snapshot__isnull=True)
                                & Q(discount_name_snapshot="")
                                & Q(discount_type_snapshot="")
                                & Q(discount_value_snapshot__isnull=True)
                                & Q(discount_amount=0)
                            )
                            | (
                                Q(discount_id_snapshot__isnull=False)
                                & ~Q(discount_name_snapshot="")
                                & Q(discount_type_snapshot__in=["percent", "fixed"])
                                & Q(discount_value_snapshot__gte=0)
                            )
                        )
                    )
                ),
                name="attendance_personal_terms_v2_discount_consistent",
            ),
        ]
        indexes = [
            models.Index(fields=["club", "terms_version"], name="att_personal_terms_version_idx"),
        ]

    def clean(self):
        super().clean()
        errors = {}
        target = self.booking or self.reservation
        if self.club_id and target is not None and target.club_id != self.club_id:
            errors["club"] = "Terms target must belong to the same club."
        if self.terms_version in COMPLETE_PERSONAL_TERMS_VERSION_VALUES:
            required = (
                "tariff_id_snapshot",
                "tariff_name_snapshot",
                "training_type_id_snapshot",
                "training_type_name_snapshot",
                "base_amount",
                "payable_amount",
                "duration_days",
                "scope",
                "component_name_snapshot",
                "component_training_type_id_snapshot",
                "component_training_type_name_snapshot",
                "component_entitlement_kind",
                "component_credits_total",
                "component_scope",
                "tariff_trainer_payout_policy",
                "component_trainer_payout_policy",
                "component_paid_amount_basis",
                "component_unit_amount_basis",
            )
            for field_name in required:
                if getattr(self, field_name) in (None, ""):
                    errors[field_name] = "Complete personal terms require this field."
            if self.component_entitlement_kind != "finite_credits" or self.component_credits_total != 1:
                errors["component_credits_total"] = "Complete personal terms require one finite credit."
            if self.tariff_trainer_payout_policy != "on_checkin":
                errors["tariff_trainer_payout_policy"] = "Complete terms require on-checkin payout."
            if self.component_trainer_payout_policy != "on_checkin":
                errors["component_trainer_payout_policy"] = "Complete terms require on-checkin payout."
        if self.terms_version != self.TermsVersion.COMPLETE_V2:
            if self.discount_amount != 0:
                errors["discount_amount"] = "Legacy personal terms require a zero discount."
        else:
            if self.base_amount is None or self.payable_amount is None:
                errors["payable_amount"] = "Complete v2 terms require monetary amounts."
            elif self.base_amount <= 0 or self.payable_amount <= 0:
                errors["payable_amount"] = "Complete v2 terms require a positive payable amount."
            elif self.discount_amount < 0 or self.base_amount - self.discount_amount != self.payable_amount:
                errors["discount_amount"] = "Complete v2 amounts must reconcile."

            no_discount_snapshot = (
                self.discount_id_snapshot is None
                and self.discount_name_snapshot == ""
                and self.discount_type_snapshot == ""
                and self.discount_value_snapshot is None
            )
            if no_discount_snapshot:
                if self.discount_amount != 0:
                    errors["discount_amount"] = "No-discount complete v2 terms require zero discount."
            else:
                required_discount_fields = (
                    "discount_id_snapshot",
                    "discount_name_snapshot",
                    "discount_type_snapshot",
                    "discount_value_snapshot",
                )
                for field_name in required_discount_fields:
                    if getattr(self, field_name) in (None, ""):
                        errors[field_name] = "Discounted complete v2 terms require this field."
                if self.discount_type_snapshot not in {"percent", "fixed"}:
                    errors["discount_type_snapshot"] = "Discount snapshot type is invalid."
                elif self.discount_value_snapshot is not None:
                    if self.discount_value_snapshot < 0:
                        errors["discount_value_snapshot"] = "Discount snapshot value is invalid."
                    elif (
                        self.discount_type_snapshot == "percent"
                        and self.discount_value_snapshot > 100
                    ):
                        errors["discount_value_snapshot"] = "Discount snapshot value is invalid."
                    elif self.base_amount is not None:
                        if self.discount_type_snapshot == "percent":
                            expected_discount = (
                                self.base_amount * self.discount_value_snapshot / 100
                            ).quantize(Decimal("0.01"))
                        else:
                            expected_discount = self.discount_value_snapshot.quantize(Decimal("0.01"))
                        if self.discount_amount != expected_discount:
                            errors["discount_amount"] = "Discount snapshot does not match amount."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Personal service terms snapshots are append-only.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Personal service terms snapshots are append-only.")

    def __str__(self):
        return f"Personal terms #{self.id} ({self.terms_version})"


def is_complete_personal_terms(terms: PersonalServiceTermsSnapshot | None) -> bool:
    return terms is not None and terms.terms_version in COMPLETE_PERSONAL_TERMS_VERSION_VALUES


def complete_personal_terms_queryset(queryset):
    return queryset.filter(terms_version__in=COMPLETE_PERSONAL_TERMS_VERSION_VALUES)


class PersonalDropInPaymentLink(TenantMixin):
    """Audit bridge for manual and bank attempts attached to a drop-in booking."""

    booking = models.ForeignKey(
        PersonalDropInBooking,
        on_delete=models.PROTECT,
        related_name="payment_links",
    )
    payment = models.OneToOneField(
        "billing.Payment",
        on_delete=models.PROTECT,
        related_name="personal_drop_in_payment_link",
    )
    bank_payment_order = models.OneToOneField(
        "billing.BankPaymentOrder",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="personal_drop_in_payment_link",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_personal_drop_in_payment_links",
    )
    idempotency_key = models.CharField(max_length=120)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["club", "idempotency_key"],
                name="uniq_personal_dropin_payment_link_idempotency",
            ),
        ]
        indexes = [
            models.Index(fields=["club", "booking", "created_at"]),
        ]

    def clean(self):
        super().clean()
        errors = {}
        for field_name in ("booking", "payment", "bank_payment_order"):
            value = getattr(self, field_name, None)
            if self.club_id and value is not None and value.club_id != self.club_id:
                errors[field_name] = "Object must belong to the same club as payment link."
        if self.booking_id and self.payment_id:
            booking_student_id = self.booking.enrollment.student_id
            if self.payment.student_id != booking_student_id:
                errors["payment"] = "Payment must belong to booking student."
            if self.payment.tariff_id != self.booking.tariff_id:
                errors["payment"] = "Payment tariff must match booking tariff."
        if self.bank_payment_order_id and self.payment_id:
            if self.bank_payment_order.payment_id != self.payment_id:
                errors["bank_payment_order"] = "Bank order must belong to linked payment."
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"Drop-in payment link #{self.id}: booking {self.booking_id}"


class PersonalPaymentMethodCorrectionQuerySet(TenantQuerySet):
    def update(self, **kwargs):
        raise ValidationError("Personal payment method corrections are append-only.")

    def delete(self):
        raise ValidationError("Personal payment method corrections are append-only.")


class PersonalPaymentMethodCorrectionManager(TenantManager):
    def get_queryset(self):
        return PersonalPaymentMethodCorrectionQuerySet(self.model, using=self._db)

    def unscoped(self):
        return self.get_queryset()


class PersonalPaymentMethodCorrection(TenantMixin):
    """Append-only lineage for a safe personal payment-method replacement.

    A payment method is never edited in place.  This record ties the terminal
    source attempt to the new manual/pay-at-visit attempt and preserves the
    server command identity that authorized that transition.
    """

    original_reservation = models.ForeignKey(
        PersonalBookingPaymentReservation,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="payment_method_corrections",
    )
    original_payment = models.ForeignKey(
        "billing.Payment",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="personal_payment_method_corrections",
    )
    original_bank_payment_order = models.ForeignKey(
        "billing.BankPaymentOrder",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="personal_payment_method_corrections",
    )
    source_terms = models.ForeignKey(
        PersonalServiceTermsSnapshot,
        on_delete=models.PROTECT,
        related_name="payment_method_corrections",
    )
    replacement_booking = models.ForeignKey(
        PersonalDropInBooking,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="payment_method_corrections",
    )
    replacement_payment_link = models.ForeignKey(
        PersonalDropInPaymentLink,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="payment_method_corrections",
    )
    replacement_payment_method = models.CharField(max_length=20)
    reason = models.CharField(max_length=500)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="personal_payment_method_corrections",
    )
    idempotency_key = models.CharField(max_length=120)
    command_fingerprint = models.CharField(max_length=64)
    command_shape = models.JSONField(default=dict)

    objects = PersonalPaymentMethodCorrectionManager()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(original_reservation__isnull=False) | Q(original_payment__isnull=False),
                name="attendance_personal_method_correction_source",
            ),
            models.UniqueConstraint(
                fields=["club", "idempotency_key"],
                name="uniq_personal_method_correction_idempotency",
            ),
        ]
        indexes = [
            models.Index(fields=["club", "original_reservation", "created_at"], name="att_method_corr_res_idx"),
            models.Index(fields=["club", "original_payment", "created_at"], name="att_method_corr_pay_idx"),
        ]

    def clean(self):
        super().clean()
        errors = {}
        for field_name in (
            "original_reservation",
            "original_payment",
            "original_bank_payment_order",
            "source_terms",
            "replacement_booking",
            "replacement_payment_link",
        ):
            value = getattr(self, field_name, None)
            if self.club_id and value is not None and value.club_id != self.club_id:
                errors[field_name] = "Correction artifacts must belong to the same club."
        if self.original_reservation_id and self.source_terms.reservation_id != self.original_reservation_id:
            errors["source_terms"] = "Correction terms must belong to the original reservation."
        if self.original_payment_id and self.replacement_payment_link_id:
            if self.replacement_payment_link.payment_id == self.original_payment_id:
                errors["replacement_payment_link"] = "Replacement payment must be a new attempt."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if not self.pk:
            return super().save(*args, **kwargs)

        persisted = type(self).objects.unscoped().filter(pk=self.pk).values(
            "club_id",
            "original_reservation_id",
            "original_payment_id",
            "original_bank_payment_order_id",
            "source_terms_id",
            "replacement_booking_id",
            "replacement_payment_link_id",
            "replacement_payment_method",
            "reason",
            "actor_id",
            "idempotency_key",
            "command_fingerprint",
            "command_shape",
        ).first()
        if persisted is None:
            raise ValidationError("Personal payment method correction does not exist.")

        immutable_fields = (
            "club_id",
            "original_reservation_id",
            "original_payment_id",
            "original_bank_payment_order_id",
            "source_terms_id",
            "replacement_payment_method",
            "reason",
            "actor_id",
            "idempotency_key",
            "command_fingerprint",
            "command_shape",
        )
        changed_immutable = any(persisted[field] != getattr(self, field) for field in immutable_fields)
        is_single_finalization = (
            persisted["replacement_booking_id"] is None
            and persisted["replacement_payment_link_id"] is None
            and self.replacement_booking_id is not None
        )
        if changed_immutable or not is_single_finalization:
            raise ValidationError("Personal payment method corrections are append-only.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Personal payment method corrections are append-only.")

    def __str__(self):
        return f"personal-method-correction:{self.club_id}:{self.idempotency_key}"


class ScheduleException(TenantMixin):
    class ExceptionType(models.TextChoices):
        CANCELLED = "cancelled", "Cancelled"
        RESCHEDULED = "rescheduled", "Rescheduled"
        SUBSTITUTE = "substitute", "Substitute"

    schedule = models.ForeignKey(
        Schedule,
        on_delete=models.CASCADE,
        related_name="exceptions",
    )
    date = models.DateField()
    exception_type = models.CharField(max_length=20, choices=ExceptionType.choices)
    reason = models.CharField(max_length=200, blank=True)
    new_date = models.DateField(null=True, blank=True)
    new_start_time = models.TimeField(null=True, blank=True)
    new_end_time = models.TimeField(null=True, blank=True)
    substitute_trainer = models.ForeignKey(
        "trainers.Trainer",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="substitutions",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["schedule", "date"],
                name="unique_exception_per_schedule_date",
            ),
        ]

    def __str__(self):
        return f"{self.schedule.group_name} - {self.date} ({self.exception_type})"


class Checkin(TenantMixin, SoftDeleteMixin):
    class Source(models.TextChoices):
        KIOSK = "kiosk", "Kiosk"
        BATCH = "batch", "Batch"
        MANUAL = "manual", "Manual"

    class NotificationPolicy(models.TextChoices):
        LIVE = "live", "Live attendance"
        SILENT_CORRECTION = "silent_correction", "Silent historical correction"
        DATED_CORRECTION = "dated_correction", "Notify with actual attendance date"

    student = models.ForeignKey(
        "students.Student",
        on_delete=models.PROTECT,
        related_name="checkins",
    )
    schedule = models.ForeignKey(
        Schedule,
        on_delete=models.PROTECT,
        related_name="checkins",
    )
    training_type = models.ForeignKey(
        "billing.TrainingType",
        on_delete=models.PROTECT,
    )
    trainer = models.ForeignKey(
        "trainers.Trainer",
        on_delete=models.PROTECT,
    )
    location = models.ForeignKey(
        "clubs.Location",
        on_delete=models.PROTECT,
    )
    date = models.DateField()
    source = models.CharField(max_length=20, choices=Source.choices)
    notification_policy = models.CharField(
        max_length=30, choices=NotificationPolicy.choices, default=NotificationPolicy.LIVE,
    )
    parent_notified_at = models.DateTimeField(null=True, blank=True)
    parent_cancellation_notified_at = models.DateTimeField(null=True, blank=True)
    subscription = models.ForeignKey(
        "billing.Subscription",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="checkins",
    )
    subscription_component = models.ForeignKey(
        "billing.SubscriptionComponent",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="checkins",
    )
    is_debt = models.BooleanField(default=False)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="cancelled_checkins",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["student", "schedule", "date"],
                condition=Q(deleted_at__isnull=True),
                name="unique_checkin_per_student_schedule_date",
            ),
        ]
        indexes = [
            models.Index(fields=["club", "date"]),
            models.Index(fields=["club", "student", "date"]),
            models.Index(fields=["club", "schedule", "date"]),
        ]

    def __str__(self):
        return f"{self.student} @ {self.schedule.group_name} {self.date}"


class StudentAttendanceCorrectionQuerySet(TenantQuerySet):
    def update(self, **kwargs):
        raise ValidationError("Attendance correction evidence is immutable.")

    def delete(self):
        raise ValidationError("Attendance correction evidence is immutable.")


class StudentAttendanceCorrectionManager(TenantManager):
    def get_queryset(self):
        return StudentAttendanceCorrectionQuerySet(self.model, using=self._db)

    def unscoped(self):
        return self.get_queryset()


class StudentAttendanceCorrection(TenantMixin):
    class Action(models.TextChoices):
        RECORD = "record", "Record attendance"
        CANCEL = "cancel", "Cancel attendance"

    checkin = models.ForeignKey(Checkin, on_delete=models.PROTECT, related_name="correction_receipts")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    action = models.CharField(max_length=12, choices=Action.choices)
    channel = models.CharField(max_length=30)
    reason = models.TextField()
    command_key = models.CharField(max_length=120)
    payload_fingerprint = models.CharField(max_length=64)
    before = models.JSONField()
    after = models.JSONField()

    objects = StudentAttendanceCorrectionManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["club", "command_key"], name="att_correction_command_uniq"),
            models.UniqueConstraint(fields=["checkin", "action"], name="att_correction_action_uniq"),
        ]
        indexes = [models.Index(fields=["club", "checkin", "created_at"], name="att_correction_history_idx")]

    def clean(self):
        super().clean()
        if self.checkin_id and self.checkin.club_id != self.club_id:
            raise ValidationError("Checkin must belong to the same club.")

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Attendance correction evidence is immutable.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Attendance correction evidence is immutable.")


class CheckinCascadeEvent(TenantMixin):
    class Effect(models.TextChoices):
        SALARY = "salary", "Salary"
        GRADE_PROGRESS = "grade_progress", "Grade progress"
        GROUP_ANALYTICS = "group_analytics", "Group analytics"
        PARENT_NOTIFICATION = "parent_notification", "Parent notification"
        RETENTION_AUTO_CLOSE = "retention_auto_close", "Retention auto close"
        POST_TRIAL_TASK = "post_trial_task", "Post-trial task"
        TRAININGS_LEFT_PUSH = "trainings_left_push", "Trainings-left push"

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"

    checkin = models.ForeignKey(
        Checkin,
        on_delete=models.PROTECT,
        related_name="cascade_events",
    )
    effect = models.CharField(max_length=40, choices=Effect.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED)
    expected = models.BooleanField(default=True)
    task_name = models.CharField(max_length=200)
    payload = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["club", "checkin", "effect"],
                name="unique_checkin_cascade_effect",
            ),
        ]
        indexes = [
            models.Index(fields=["club", "checkin", "status"]),
            models.Index(fields=["club", "effect", "status"]),
        ]

    def clean(self):
        super().clean()
        if self.club_id and self.checkin_id and self.checkin.club_id != self.club_id:
            raise ValidationError({"checkin": "Check-in does not belong to cascade event club."})

    def __str__(self):
        return f"{self.checkin_id}:{self.effect}:{self.status}"


class KioskDevice(models.Model):
    """Device token for kiosk tablet authentication (not per-user, per-club)."""

    club = models.ForeignKey("clubs.Club", on_delete=models.PROTECT, related_name="kiosk_devices")
    token = models.CharField(max_length=64, unique=True, db_index=True)
    pin_code = models.CharField(max_length=6, blank=True, default="")
    pin_expires_at = models.DateTimeField(null=True, blank=True)
    activation_failed_attempts = models.PositiveSmallIntegerField(default=0)
    activation_locked_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    activated_at = models.DateTimeField(auto_now_add=True)
    deactivated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["pin_code"],
                condition=Q(is_active=True, pin_code__gt=""),
                name="uniq_active_kiosk_pin_code",
            ),
        ]
        indexes = [
            models.Index(fields=["club", "is_active"]),
        ]

    def __str__(self):
        return f"Kiosk {self.club} ({'active' if self.is_active else 'inactive'})"


class GroupSession(TenantMixin):
    class CloseSource(models.TextChoices):
        TRAINER_REVIEW = "trainer_review", "Trainer review"
        BATCH = "batch", "Batch correction"
        SYSTEM = "system", "System"

    schedule = models.ForeignKey(
        Schedule,
        on_delete=models.PROTECT,
        related_name="sessions",
    )
    date = models.DateField()
    trainer = models.ForeignKey(
        "trainers.Trainer",
        on_delete=models.PROTECT,
    )
    attendee_count = models.PositiveIntegerField(default=0)
    topic_tags = models.JSONField(default=list)
    notes = models.TextField(blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="closed_group_sessions",
    )
    close_source = models.CharField(
        max_length=32,
        choices=CloseSource.choices,
        blank=True,
        default="",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["schedule", "date"],
                name="unique_group_session_per_schedule_date",
            ),
        ]
        indexes = [
            models.Index(fields=["club", "date", "closed_at"], name="att_gsession_closed_idx"),
        ]

    def __str__(self):
        return f"{self.schedule.group_name} {self.date} ({self.attendee_count})"
