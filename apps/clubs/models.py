from datetime import time

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models

from apps.common.managers import TenantManager, TenantQuerySet
from apps.common.models import BaseModel

hex_color_validator = RegexValidator(
    regex=r"^#[0-9A-Fa-f]{6}$",
    message="Must be hex color #RRGGBB",
)


class Club(BaseModel):
    name = models.CharField(max_length=200)
    city = models.CharField(max_length=100)
    disciplines = models.JSONField(default=list)
    is_active = models.BooleanField(default=True)
    timezone = models.CharField(max_length=50, default="Europe/Moscow")

    def __str__(self):
        return self.name


class Location(BaseModel):
    club = models.ForeignKey(Club, on_delete=models.PROTECT, related_name="locations")
    name = models.CharField(max_length=200)
    address = models.TextField(blank=True)

    def __str__(self):
        return f"{self.club.name} - {self.name}"


class ClubMembership(BaseModel):
    class Role(models.TextChoices):
        OWNER = "owner", "Owner"
        ADMIN = "admin", "Admin"
        TRAINER = "trainer", "Trainer"
        STUDENT = "student", "Student"
        PARENT = "parent", "Parent"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="club_memberships",
    )
    club = models.ForeignKey(Club, on_delete=models.PROTECT, related_name="memberships")
    role = models.CharField(max_length=20, choices=Role.choices)
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = [("user", "club")]

    def __str__(self):
        return f"{self.user} - {self.club.name} ({self.role})"


class ClubSettings(BaseModel):
    class CommercialJourneyProtocol(models.TextChoices):
        V1 = "v1", "Commercial journey protocol v1"
        V2 = "v2", "Commercial journey protocol v2"

    club = models.OneToOneField(Club, on_delete=models.PROTECT, related_name="settings")
    unified_client_journey_enabled = models.BooleanField(default=False)
    # A tenant never receives a protocol upgrade from a migration.  v2 needs
    # an explicit, audited rollout decision after the backend and clients are
    # capable of safely rejecting or draining older command shapes.
    commercial_journey_protocol_version = models.CharField(
        max_length=2,
        choices=CommercialJourneyProtocol.choices,
        default=CommercialJourneyProtocol.V1,
    )
    freeze_enabled = models.BooleanField(default=True)
    freeze_max_days = models.PositiveIntegerField(default=30)
    freeze_max_count = models.PositiveIntegerField(null=True, blank=True)  # null = unlimited
    min_trainings_to_freeze = models.PositiveIntegerField(default=2)
    feedback_delay_hours = models.PositiveIntegerField(default=2)
    max_push_per_week = models.PositiveIntegerField(default=3)
    quiet_hours_start = models.TimeField(default=time(21, 0))
    quiet_hours_end = models.TimeField(default=time(9, 0))

    # Branding
    primary_color = models.CharField(
        max_length=7, default="#000000", validators=[hex_color_validator],
    )
    accent_color = models.CharField(
        max_length=7, default="#FF6B00", validators=[hex_color_validator],
    )
    club_name_display = models.CharField(max_length=200, blank=True, default="")
    logo_url = models.URLField(blank=True, default="")
    # Public-by-design: served from /media/club_logos/* without authentication.
    # Required for login page (pre-auth branding) and PWA CSS variables.
    # Validation enforced in apps/htmx_admin/views/settings.py
    # (PNG/JPG/WEBP only, max 2MB).
    logo_file = models.ImageField(upload_to="club_logos/", blank=True, null=True)

    @property
    def logo_src(self) -> str:
        """Resolved logo URL: uploaded file takes priority over external URL."""
        if self.logo_file:
            return self.logo_file.url
        return self.logo_url or ""

    def __str__(self):
        return f"Settings for {self.club.name}"


class _AppendOnlyCommercialJourneyTransitionQuerySet(TenantQuerySet):
    def update(self, **kwargs):
        raise ValidationError("Commercial journey protocol transition receipts are immutable.")

    def delete(self):
        raise ValidationError("Commercial journey protocol transition receipts are immutable.")


class _AppendOnlyCommercialJourneyTransitionManager(TenantManager):
    def get_queryset(self):
        return _AppendOnlyCommercialJourneyTransitionQuerySet(self.model, using=self._db)

    def unscoped(self):
        return self.get_queryset()


class CommercialJourneyProtocolTransition(BaseModel):
    """Append-only tenant receipt for an explicit protocol transition."""

    club = models.ForeignKey(
        Club,
        on_delete=models.PROTECT,
        related_name="commercial_journey_protocol_transitions",
    )
    previous_version = models.CharField(
        max_length=2,
        choices=ClubSettings.CommercialJourneyProtocol.choices,
    )
    target_version = models.CharField(
        max_length=2,
        choices=ClubSettings.CommercialJourneyProtocol.choices,
    )
    rationale = models.CharField(max_length=500)
    idempotency_key = models.CharField(max_length=120)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="commercial_journey_protocol_transitions",
    )
    readiness_snapshot = models.JSONField(default=dict)
    objects = _AppendOnlyCommercialJourneyTransitionManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["club", "idempotency_key"],
                name="uniq_club_commercial_protocol_transition_key",
            )
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError(
                "Commercial journey protocol transition receipts are immutable."
            )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(
            "Commercial journey protocol transition receipts are immutable."
        )
