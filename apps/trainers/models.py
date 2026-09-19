from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from apps.common.managers import TenantManager, TenantQuerySet
from apps.common.models import TenantMixin


class Trainer(TenantMixin):
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    phone = models.CharField(max_length=20, blank=True)
    is_active = models.BooleanField(default=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="trainer_profile",
    )
    locations = models.ManyToManyField(
        "clubs.Location",
        through="TrainerLocation",
        related_name="trainers",
    )

    def __str__(self):
        return f"{self.first_name} {self.last_name}"


class TrainerLocation(TenantMixin):
    trainer = models.ForeignKey(Trainer, on_delete=models.PROTECT, related_name="trainer_locations")
    location = models.ForeignKey("clubs.Location", on_delete=models.PROTECT, related_name="trainer_locations")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["trainer", "location"],
                name="unique_trainer_per_location",
            ),
        ]

    def __str__(self):
        return f"{self.trainer} @ {self.location}"


class TrainerRate(TenantMixin):
    """Flexible per-(trainer, location, training_type) rate.

    Replaces the hardcoded rate_group / rate_personal / rate_mini_group
    columns on TrainerLocation so owners can add new TrainingType rows
    in settings without code/migration changes.
    """

    trainer = models.ForeignKey(Trainer, on_delete=models.PROTECT, related_name="rates")
    location = models.ForeignKey(
        "clubs.Location",
        on_delete=models.PROTECT,
        related_name="trainer_rates",
    )
    training_type = models.ForeignKey(
        "billing.TrainingType",
        on_delete=models.PROTECT,
        related_name="trainer_rates",
    )
    percent = models.DecimalField(max_digits=5, decimal_places=2)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["club", "trainer", "location", "training_type"],
                name="unique_rate_per_trainer_location_type",
            ),
        ]
        indexes = [
            models.Index(fields=["club", "trainer", "location", "training_type"]),
        ]

    def __str__(self):
        return f"{self.trainer} @ {self.location} / {self.training_type}: {self.percent}%"


class TrainerEarning(TenantMixin):
    class EarningType(models.TextChoices):
        GROUP = "group", "Групповое"
        PERSONAL = "personal", "Персональное"
        MINI_GROUP = "mini_group", "Мини-группа"

    class Source(models.TextChoices):
        CHECKIN = "checkin", "Чек-ин"
        SALE = "sale", "Продажа"

    trainer = models.ForeignKey(Trainer, on_delete=models.PROTECT, related_name="earnings")
    checkin = models.OneToOneField(
        "attendance.Checkin",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
    )
    payment = models.ForeignKey(
        "billing.Payment",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="earnings",
    )
    subscription_component = models.ForeignKey(
        "billing.SubscriptionComponent",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="trainer_earnings",
    )
    earning_source = models.CharField(
        max_length=10,
        choices=Source.choices,
        default=Source.CHECKIN,
    )
    earning_type = models.CharField(max_length=20, choices=EarningType.choices)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    rate_percent = models.DecimalField(max_digits=5, decimal_places=2)
    subscription_price = models.DecimalField(max_digits=10, decimal_places=2, null=True)
    payout_policy_snapshot = models.CharField(max_length=20, blank=True, default="")
    component_id_snapshot = models.PositiveBigIntegerField(null=True, blank=True)
    component_paid_amount_basis_snapshot = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
    )
    cancelled = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["payment"],
                condition=models.Q(payment__isnull=False, subscription_component__isnull=True),
                name="unique_legacy_sale_earning_per_payment",
            ),
            models.UniqueConstraint(
                fields=["payment", "subscription_component"],
                condition=models.Q(payment__isnull=False, subscription_component__isnull=False),
                name="unique_sale_earning_per_payment_component",
            ),
        ]

    def __str__(self):
        return f"{self.trainer} - {self.amount} ({self.earning_type})"


class TrainerPackageAllocation(TenantMixin):
    class Source(models.TextChoices):
        PAYMENT = "payment", "Payment"
        OPENING = "opening", "Reviewed opening import"
        MANUAL_SUBSCRIPTION = "manual_subscription", "Manual subscription"
        MANUAL_TRANSFER = "manual_transfer", "Manual transfer"

    subscription = models.ForeignKey(
        "billing.Subscription",
        on_delete=models.PROTECT,
        related_name="trainer_allocations",
    )
    payment = models.ForeignKey(
        "billing.Payment",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="trainer_allocations",
    )
    student = models.ForeignKey(
        "students.Student",
        on_delete=models.PROTECT,
        related_name="trainer_package_allocations",
    )
    tariff = models.ForeignKey(
        "billing.Tariff",
        on_delete=models.PROTECT,
        related_name="trainer_package_allocations",
    )
    training_type = models.ForeignKey(
        "billing.TrainingType",
        on_delete=models.PROTECT,
        related_name="trainer_package_allocations",
    )
    owner_trainer = models.ForeignKey(
        Trainer,
        on_delete=models.PROTECT,
        related_name="owned_package_allocations",
    )
    source = models.CharField(max_length=32, choices=Source.choices, default=Source.MANUAL_SUBSCRIPTION)
    sessions_total_snapshot = models.PositiveIntegerField(null=True, blank=True)
    sessions_remaining_snapshot = models.PositiveIntegerField(null=True, blank=True)
    amount_snapshot = models.DecimalField(max_digits=10, decimal_places=2)
    is_active = models.BooleanField(default=True)
    activated_at = models.DateTimeField(null=True, blank=True)
    deactivated_at = models.DateTimeField(null=True, blank=True)
    deactivated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="deactivated_trainer_package_allocations",
    )
    transferred_from = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="transfers",
    )
    transfer_reason = models.TextField(blank=True, default="")
    note = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="created_trainer_package_allocations",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["club", "subscription"],
                condition=models.Q(is_active=True),
                name="unique_active_trainer_package_allocation",
            ),
        ]
        indexes = [
            models.Index(fields=["club", "owner_trainer", "is_active"]),
            models.Index(fields=["club", "subscription", "created_at"]),
        ]

    def clean(self):
        super().clean()
        self._validate_same_club("subscription")
        self._validate_same_club("payment")
        self._validate_same_club("student")
        self._validate_same_club("tariff")
        self._validate_same_club("training_type")
        self._validate_same_club("owner_trainer")
        self._validate_same_club("transferred_from")
        self._validate_relationship_consistency()

    def _validate_same_club(self, field_name: str) -> None:
        obj = getattr(self, field_name, None)
        if self.club_id and obj is not None and getattr(obj, "club_id", self.club_id) != self.club_id:
            raise ValidationError({field_name: "Object must belong to the same club."})

    def _validate_relationship_consistency(self) -> None:
        errors: dict[str, str] = {}
        if self.subscription_id:
            if self.student_id and self.subscription.student_id != self.student_id:
                errors["student"] = "Student must match subscription student."
            if self.tariff_id and self.subscription.tariff_id != self.tariff_id:
                errors["tariff"] = "Tariff must match subscription tariff."
        if self.tariff_id and self.training_type_id and self.tariff.training_type_id != self.training_type_id:
            has_matching_component = self.tariff.components.filter(
                club_id=self.club_id,
                training_type_id=self.training_type_id,
                is_active=True,
            ).exists()
            if not has_matching_component:
                errors["training_type"] = "Training type must match tariff training type or component."
        if self.payment_id and self.subscription_id and self.payment.subscription_id != self.subscription_id:
            errors["payment"] = "Payment must belong to the same subscription."
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"Package allocation #{self.id}: subscription {self.subscription_id} -> trainer {self.owner_trainer_id}"


class TrainerEarningAdjustment(TenantMixin):
    class Direction(models.TextChoices):
        CREDIT = "credit", "Credit"
        DEBIT = "debit", "Debit"
        INFO = "info", "Info"

    class Kind(models.TextChoices):
        PACKAGE_TRANSFER = "package_transfer", "Package transfer"
        MANUAL_ADJUSTMENT = "manual_adjustment", "Manual adjustment"
        REVERSAL = "reversal", "Reversal"
        REFUND = "refund", "Payment refund"
        LATE_DROP_IN_CREDIT = "late_drop_in_credit", "Late drop-in settlement credit"
        CHECKIN_CANCELLATION_DEBIT = "checkin_cancellation_debit", "Check-in cancellation debit"

    trainer = models.ForeignKey(Trainer, on_delete=models.PROTECT, related_name="earning_adjustments")
    amount_basis_snapshot = models.DecimalField(max_digits=10, decimal_places=2)
    payable_amount_delta = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    affects_payroll = models.BooleanField(default=False)
    direction = models.CharField(max_length=10, choices=Direction.choices, default=Direction.INFO)
    kind = models.CharField(max_length=32, choices=Kind.choices)
    effective_date = models.DateField()
    source_checkin = models.ForeignKey(
        "attendance.Checkin",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="trainer_earning_adjustments",
    )
    source_subscription = models.ForeignKey(
        "billing.Subscription",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="trainer_earning_adjustments",
    )
    source_payment = models.ForeignKey(
        "billing.Payment",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="trainer_earning_adjustments",
    )
    source_refund = models.ForeignKey(
        "billing.PaymentRefund",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="trainer_earning_adjustments",
    )
    source_earning = models.ForeignKey(
        TrainerEarning,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="refund_adjustments",
    )
    counterparty_trainer = models.ForeignKey(
        Trainer,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="counterparty_earning_adjustments",
    )
    correction_group_id = models.UUIDField(null=True, blank=True, db_index=True)
    idempotency_key = models.CharField(max_length=120, blank=True, default="")
    reason = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="trainer_earning_adjustments",
    )
    reversal_of = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversals",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["club", "source_checkin", "trainer", "kind", "direction"],
                condition=models.Q(kind="package_transfer", source_checkin__isnull=False),
                name="unique_package_transfer_adjustment",
            ),
            models.UniqueConstraint(
                fields=["club", "reversal_of", "kind"],
                condition=models.Q(kind="reversal", reversal_of__isnull=False),
                name="unique_package_transfer_reversal",
            ),
            models.UniqueConstraint(
                fields=["club", "correction_group_id", "direction"],
                condition=models.Q(kind="manual_adjustment", correction_group_id__isnull=False),
                name="unique_manual_adjustment_group_direction",
            ),
            models.UniqueConstraint(
                fields=["club", "idempotency_key", "kind", "direction"],
                condition=models.Q(kind="manual_adjustment") & ~models.Q(idempotency_key=""),
                name="unique_manual_adjustment_idempotency_direction",
            ),
            models.UniqueConstraint(
                fields=["club", "source_checkin", "trainer", "kind", "direction"],
                condition=models.Q(
                    kind="manual_adjustment",
                    direction="debit",
                    source_checkin__isnull=False,
                ),
                name="unique_manual_adjustment_checkin_debit",
            ),
            models.UniqueConstraint(
                fields=["club", "source_payment", "trainer", "kind", "direction"],
                condition=models.Q(
                    kind="manual_adjustment",
                    direction="debit",
                    source_payment__isnull=False,
                ),
                name="unique_manual_adjustment_payment_debit",
            ),
            models.UniqueConstraint(
                fields=["club", "source_refund", "source_earning", "trainer"],
                condition=models.Q(
                    kind="refund",
                    source_refund__isnull=False,
                    source_earning__isnull=False,
                ),
                name="unique_refund_adjustment_per_earning_trainer",
            ),
            models.UniqueConstraint(
                fields=["club", "source_checkin", "source_payment", "trainer", "kind", "direction"],
                condition=models.Q(
                    kind="late_drop_in_credit",
                    source_checkin__isnull=False,
                    source_payment__isnull=False,
                ),
                name="unique_late_dropin_credit",
            ),
            models.UniqueConstraint(
                fields=["club", "source_checkin", "source_earning", "trainer", "kind", "direction"],
                condition=models.Q(
                    kind="checkin_cancellation_debit",
                    source_checkin__isnull=False,
                    source_earning__isnull=False,
                ),
                name="unique_checkin_cancel_earning_debit",
            ),
            models.UniqueConstraint(
                fields=["club", "source_checkin", "reversal_of", "trainer", "kind", "direction"],
                condition=models.Q(
                    kind="checkin_cancellation_debit",
                    source_checkin__isnull=False,
                    reversal_of__isnull=False,
                ),
                name="unique_checkin_cancel_late_debit",
            ),
        ]
        indexes = [
            models.Index(fields=["club", "trainer", "effective_date", "kind"]),
            models.Index(fields=["club", "correction_group_id"]),
            models.Index(fields=["club", "idempotency_key"]),
        ]

    def clean(self):
        super().clean()
        self._validate_same_club("trainer")
        self._validate_same_club("source_checkin")
        self._validate_same_club("source_subscription")
        self._validate_same_club("source_payment")
        self._validate_same_club("source_refund")
        self._validate_same_club("source_earning")
        self._validate_same_club("counterparty_trainer")
        self._validate_same_club("reversal_of")

    def _validate_same_club(self, field_name: str) -> None:
        obj = getattr(self, field_name, None)
        if self.club_id and obj is not None and getattr(obj, "club_id", self.club_id) != self.club_id:
            raise ValidationError({field_name: "Object must belong to the same club."})

    def __str__(self):
        return f"Trainer adjustment #{self.id}: {self.kind} {self.payable_amount_delta}"


class TrainerPayrollPeriodClose(TenantMixin):
    period_start = models.DateField()
    period_end = models.DateField()
    closed_at = models.DateTimeField(auto_now_add=True)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="trainer_payroll_period_closes",
    )
    reason = models.TextField()
    salary_total_snapshot = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    trainer_totals_snapshot = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["club", "period_start", "period_end"]),
            models.Index(fields=["club", "closed_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(period_end__gte=models.F("period_start")),
                name="trainer_payroll_close_valid_period",
            ),
        ]

    def clean(self):
        super().clean()
        if self.period_end and self.period_start and self.period_end < self.period_start:
            raise ValidationError({"period_end": "Period end cannot be before period start."})

    def __str__(self):
        return f"Payroll close {self.club_id}: {self.period_start}..{self.period_end}"


class _SettlementJournalQuerySet(TenantQuerySet):
    def update(self, **kwargs):
        raise ValidationError("Settlement evidence is append-only.")

    def delete(self):
        raise ValidationError("Settlement evidence is append-only.")

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValidationError("Settlement evidence is append-only.")


class _SettlementJournalManager(TenantManager):
    def get_queryset(self):
        return _SettlementJournalQuerySet(self.model, using=self._db)

    def unscoped(self):
        return self.get_queryset()


class _SettlementJournal(TenantMixin):
    objects = _SettlementJournalManager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Settlement evidence is append-only.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Settlement evidence is append-only.")


class TrainerSettlementEntry(_SettlementJournal):
    class Kind(models.TextChoices):
        OPENING = "opening", "Начальная сверка"
        OPENING_CORRECTION = "opening_correction", "Исправление начальной сверки"
        PAYOUT = "payout", "Выплата"
        PAYOUT_REVERSAL = "payout_reversal", "Отмена выплаты"

    trainer = models.ForeignKey(Trainer, on_delete=models.PROTECT, related_name="settlement_entries")
    kind = models.CharField(max_length=24, choices=Kind.choices)
    effective_on = models.DateField()
    balance_delta = models.DecimalField(max_digits=14, decimal_places=2)
    amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    payment_method = models.CharField(max_length=12, blank=True, default="")
    opening = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="subsequent_entries"
    )
    reversal_of = models.OneToOneField("self", on_delete=models.PROTECT, null=True, blank=True, related_name="reversal")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="trainer_settlement_entries"
    )
    reason = models.TextField()
    channel = models.CharField(max_length=16, default="admin")
    source_namespace = models.CharField(max_length=120)
    source_key = models.CharField(max_length=160)
    payload_fingerprint = models.CharField(max_length=64)
    command_payload = models.JSONField(default=dict)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["club", "source_namespace", "kind", "source_key"], name="settlement_source_unique"
            ),
            models.UniqueConstraint(
                fields=["club", "trainer"], condition=models.Q(kind="opening"), name="settlement_one_opening"
            ),
            models.CheckConstraint(
                name="settlement_typed_effect",
                condition=(
                    models.Q(
                        kind="opening",
                        amount__isnull=True,
                        opening__isnull=True,
                        reversal_of__isnull=True,
                        payment_method="",
                    )
                    | models.Q(
                        kind="opening_correction",
                        amount__isnull=True,
                        opening__isnull=False,
                        reversal_of__isnull=True,
                        payment_method="",
                    )
                    | models.Q(
                        kind="payout",
                        amount__gt=0,
                        amount__isnull=False,
                        opening__isnull=False,
                        reversal_of__isnull=True,
                        balance_delta=-models.F("amount"),
                        payment_method__in=["cash", "transfer"],
                    )
                    | models.Q(
                        kind="payout_reversal",
                        amount__gt=0,
                        amount__isnull=False,
                        opening__isnull=False,
                        reversal_of__isnull=False,
                        balance_delta=models.F("amount"),
                        payment_method="",
                    )
                ),
            ),
        ]
        indexes = [models.Index(fields=["club", "trainer", "effective_on"])]


class TrainerSettlementReconciliation(_SettlementJournal):
    trainer = models.ForeignKey(Trainer, on_delete=models.PROTECT, related_name="settlement_reconciliations")
    opening = models.ForeignKey(TrainerSettlementEntry, on_delete=models.PROTECT, related_name="reconciliations")
    event_key = models.CharField(max_length=180)
    effective_on = models.DateField()
    suggested_delta = models.DecimalField(max_digits=14, decimal_places=2)
    evidence = models.JSONField(default=dict)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["club", "trainer", "event_key"], name="settlement_event_unique")]
        indexes = [models.Index(fields=["club", "trainer"])]


class TrainerSettlementResolution(_SettlementJournal):
    case = models.OneToOneField(TrainerSettlementReconciliation, on_delete=models.PROTECT, related_name="resolution")
    action = models.CharField(
        max_length=24, choices=[("already_included", "Уже учтено"), ("adjust_opening", "Исправить остаток")]
    )
    correction = models.OneToOneField(
        TrainerSettlementEntry,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reconciliation_resolution",
    )
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="settlement_resolutions")
    reason = models.TextField()
    source_namespace = models.CharField(max_length=120)
    source_key = models.CharField(max_length=160)
    payload_fingerprint = models.CharField(max_length=64)
    command_payload = models.JSONField(default=dict)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["club", "source_namespace", "source_key"], name="settlement_resolution_unique"
            ),
            models.CheckConstraint(
                name="settlement_resolution_kind",
                condition=(
                    models.Q(action="already_included", correction__isnull=True)
                    | models.Q(action="adjust_opening", correction__isnull=False)
                ),
            ),
        ]
