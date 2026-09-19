from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, router, transaction

from apps.common.managers import TenantManager, TenantQuerySet
from apps.common.models import BaseModel, SoftDeleteMixin, TenantMixin

_TARIFF_REVISION_SEALED_FIELDS = frozenset(
    {
        "club",
        "club_id",
        "name",
        "training_type",
        "training_type_id",
        "price",
        "trainings_limit",
        "duration_days",
        "scope",
        "location",
        "location_id",
        "personal_booking_trainer",
        "personal_booking_trainer_id",
        "trainer_payout_policy",
    }
)
_TARIFF_COMPONENT_REVISION_SEALED_FIELDS = frozenset(
    {
        "club",
        "club_id",
        "tariff",
        "tariff_id",
        "name",
        "training_type",
        "training_type_id",
        "entitlement_kind",
        "credits_total",
        "weekly_limit",
        "scope",
        "location",
        "location_id",
        "trainer_payout_policy",
        "paid_amount_basis",
        "sort_order",
        "is_active",
    }
)
_TARIFF_REVISION_LIFECYCLE_FIELDS = frozenset(
    {"is_active", "is_personal_booking_default"}
)
_TARIFF_REVISION_GUARDED_FIELDS = (
    _TARIFF_REVISION_SEALED_FIELDS | _TARIFF_REVISION_LIFECYCLE_FIELDS
)


def _validate_tariff_lifecycle_updates(updates):
    for field_name in _TARIFF_REVISION_LIFECYCLE_FIELDS:
        if field_name in updates and not isinstance(updates[field_name], bool):
            raise ValidationError(f"{field_name} must be a boolean value.")


def _has_tariff_revision_edge(*, club_id, tariff_id):
    if not club_id or not tariff_id:
        return False
    from django.db.models import Q

    return TariffPriceRevision.objects.for_club(club_id).filter(
        Q(source_tariff_id=tariff_id) | Q(target_tariff_id=tariff_id),
    ).exists()


def _has_outgoing_tariff_revision(*, club_id, tariff_id):
    if not club_id or not tariff_id:
        return False
    return TariffPriceRevision.objects.for_club(club_id).filter(source_tariff_id=tariff_id).exists()


def _has_incoming_tariff_revision(*, club_id, tariff_id):
    if not club_id or not tariff_id:
        return False
    return TariffPriceRevision.objects.for_club(club_id).filter(target_tariff_id=tariff_id).exists()


def _coerce_component_tariff_id(value):
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValidationError("Tariff component parent must be a concrete tariff id.")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError("Tariff component parent must be a concrete tariff id.") from exc


def _coerce_bulk_fk_id(value, *, field_name):
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValidationError({field_name: "A concrete related-object id is required."})
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError({field_name: "A concrete related-object id is required."}) from exc


def _lock_tariff_bulk_links(*, using, objects):
    """Lock linked catalog rows in the same type -> trainer -> location order."""
    from apps.clubs.models import Location
    from apps.trainers.models import Trainer

    training_type_ids = sorted(
        {
            training_type_id
            for training_type_id in (
                _coerce_bulk_fk_id(obj.training_type_id, field_name="training_type")
                for obj in objects
            )
            if training_type_id is not None
        }
    )
    trainer_ids = sorted(
        {
            trainer_id
            for trainer_id in (
                _coerce_bulk_fk_id(
                    getattr(obj, "personal_booking_trainer_id", None),
                    field_name="personal_booking_trainer",
                )
                for obj in objects
            )
            if trainer_id is not None
        }
    )
    location_ids = sorted(
        {
            location_id
            for location_id in (
                _coerce_bulk_fk_id(obj.location_id, field_name="location")
                for obj in objects
            )
            if location_id is not None
        }
    )
    training_types = {
        row["id"]: row["club_id"]
        for row in TrainingType.objects.using(using)
        .select_for_update(of=("self",))
        .filter(id__in=training_type_ids)
        .values("id", "club_id")
    }
    trainers = {
        row["id"]: row["club_id"]
        for row in Trainer.objects.using(using)
        .select_for_update(of=("self",))
        .filter(id__in=trainer_ids)
        .values("id", "club_id")
    }
    locations = {
        row["id"]: row["club_id"]
        for row in Location.objects.using(using)
        .select_for_update(of=("self",))
        .filter(id__in=location_ids)
        .values("id", "club_id")
    }
    return training_types, trainers, locations


def _validate_tariff_bulk_objects(*, using, objects):
    training_types, trainers, locations = _lock_tariff_bulk_links(using=using, objects=objects)
    for tariff in objects:
        club_id = _coerce_bulk_fk_id(tariff.club_id, field_name="club")
        training_type_id = _coerce_bulk_fk_id(tariff.training_type_id, field_name="training_type")
        if club_id is None or training_types.get(training_type_id) != club_id:
            raise ValidationError({"training_type": "Training type must belong to the tariff club."})
        location_id = _coerce_bulk_fk_id(tariff.location_id, field_name="location")
        if location_id is not None and locations.get(location_id) != club_id:
            raise ValidationError({"location": "Location must belong to the tariff club."})
        trainer_id = _coerce_bulk_fk_id(
            tariff.personal_booking_trainer_id,
            field_name="personal_booking_trainer",
        )
        if trainer_id is not None and trainers.get(trainer_id) != club_id:
            raise ValidationError(
                {"personal_booking_trainer": "Personal booking trainer must belong to the tariff club."}
            )
        tariff.full_clean()


def _validate_component_bulk_objects(*, objects, parent_clubs, links):
    training_types, _trainers, locations = links
    for component in objects:
        tariff_id = _coerce_component_tariff_id(component.tariff_id)
        parent_club_id = parent_clubs.get(tariff_id)
        club_id = _coerce_bulk_fk_id(component.club_id, field_name="club")
        if parent_club_id is None:
            raise ValidationError({"tariff": "Tariff parent does not exist."})
        if club_id != parent_club_id:
            raise ValidationError({"club": "Tariff component must belong to its tariff club."})
        training_type_id = _coerce_bulk_fk_id(component.training_type_id, field_name="training_type")
        if training_types.get(training_type_id) != club_id:
            raise ValidationError({"training_type": "Training type must belong to the component club."})
        location_id = _coerce_bulk_fk_id(component.location_id, field_name="location")
        if location_id is not None and locations.get(location_id) != club_id:
            raise ValidationError({"location": "Location must belong to the component club."})
        component.full_clean()


def _lock_component_parent_tariffs(*, using, tariff_ids):
    ids = sorted({tariff_id for tariff_id in tariff_ids if tariff_id is not None})
    if not ids:
        return {}
    rows = (
        Tariff.objects.unscoped()
        .using(using)
        .select_for_update(of=("self",))
        .filter(id__in=ids)
        .values("id", "club_id")
    )
    return {row["id"]: row["club_id"] for row in rows}


def _tariff_edge_update_blocked(*, club_id, tariff_id, updates):
    """Block contract rewrites and old-version reactivation/defaulting."""
    _validate_tariff_lifecycle_updates(updates)
    outgoing = _has_outgoing_tariff_revision(club_id=club_id, tariff_id=tariff_id)
    incoming = _has_incoming_tariff_revision(club_id=club_id, tariff_id=tariff_id)
    if not outgoing and not incoming:
        return False
    update_fields = set(updates)
    if update_fields.intersection(_TARIFF_REVISION_SEALED_FIELDS):
        return True
    if outgoing:
        for field_name in _TARIFF_REVISION_LIFECYCLE_FIELDS:
            if field_name in updates and updates[field_name] is not False:
                return True
    return False


class TrainingType(TenantMixin):
    class Kind(models.TextChoices):
        PERSONAL = "personal", "Персональная"
        MINI_GROUP = "mini_group", "Мини-группа"
        GROUP = "group", "Групповая"

    name = models.CharField(max_length=100)
    slug = models.SlugField(max_length=100)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.GROUP)
    is_active = models.BooleanField(default=True)
    grade_system = models.ForeignKey(
        "grades.GradeSystem",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="training_types",
    )
    drop_in_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
    )
    trial_free = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(drop_in_price__isnull=True) | models.Q(drop_in_price__gt=0),
                name="billing_trainingtype_dropin_positive",
            ),
            models.UniqueConstraint(
                fields=["club", "slug"],
                name="unique_training_type_per_club",
            ),
        ]

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        if self.club_id and self.grade_system_id and self.grade_system.club_id != self.club_id:
            raise ValidationError({"grade_system": "Grade system must belong to the same club as training type."})


class TariffQuerySet(TenantQuerySet):
    def bulk_create(self, objs, **kwargs):
        if kwargs.get("update_conflicts"):
            raise ValidationError("Tariff upserts are disabled while tariff revisions are supported.")
        objects = list(objs)
        if not objects:
            return super().bulk_create(objects, **kwargs)
        with transaction.atomic(using=self.db):
            _validate_tariff_bulk_objects(using=self.db, objects=objects)
            return super().bulk_create(objects, **kwargs)

    def update(self, **kwargs):
        if not _TARIFF_REVISION_GUARDED_FIELDS.intersection(kwargs):
            return super().update(**kwargs)

        _validate_tariff_lifecycle_updates(kwargs)
        using = self.db
        with transaction.atomic(using=using):
            tariff_ids = list(self.values_list("id", flat=True))
            tariff_rows = list(
                self.model._base_manager.using(using)
                .select_for_update()
                .filter(id__in=tariff_ids)
                .values("id", "club_id")
            )
            if any(
                _tariff_edge_update_blocked(
                    club_id=row["club_id"],
                    tariff_id=row["id"],
                    updates=kwargs,
                )
                for row in tariff_rows
            ):
                raise ValidationError("Tariff contracts sealed by a price revision are immutable.")
            return super().update(**kwargs)


class TariffManager(TenantManager):
    def get_queryset(self):
        return TariffQuerySet(self.model, using=self._db)

    def unscoped(self):
        return self.get_queryset()


class Tariff(TenantMixin):
    class Scope(models.TextChoices):
        CLUB = "club", "Club"
        LOCATION = "location", "Location"

    class PayoutPolicy(models.TextChoices):
        NONE = "none", "No trainer payout"
        ON_CHECKIN = "on_checkin", "Per check-in"
        ON_PAYMENT = "on_payment", "On payment"

    name = models.CharField(max_length=200)
    training_type = models.ForeignKey(
        TrainingType,
        on_delete=models.PROTECT,
        related_name="tariffs",
    )
    price = models.DecimalField(max_digits=10, decimal_places=2)
    trainings_limit = models.PositiveIntegerField(null=True, blank=True)
    duration_days = models.PositiveIntegerField()
    scope = models.CharField(max_length=20, choices=Scope.choices, default=Scope.CLUB)
    location = models.ForeignKey(
        "clubs.Location",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="tariffs",
    )
    is_active = models.BooleanField(default=True)
    # A personal availability slot deliberately does not carry a mutable price
    # or tariff foreign key.  This marker selects the current one-session
    # offer for its type/scope instead.
    is_personal_booking_default = models.BooleanField(default=False)
    personal_booking_trainer = models.ForeignKey(
        "trainers.Trainer",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="personal_booking_tariffs",
    )
    description = models.TextField(blank=True)
    trainer_payout_policy = models.CharField(
        max_length=20,
        choices=PayoutPolicy.choices,
        blank=True,
        default="",
    )

    objects = TariffManager()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(price__gt=0),
                name="billing_tariff_price_positive",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(is_personal_booking_default=False)
                    | models.Q(scope="location", location__isnull=False)
                    | models.Q(scope="club", location__isnull=True)
                ),
                name="billing_tariff_scope_location_consistent",
            ),
            models.UniqueConstraint(
                fields=["club", "training_type"],
                condition=models.Q(
                    is_personal_booking_default=True,
                    scope="club",
                    personal_booking_trainer__isnull=True,
                ),
                name="uniq_personal_booking_generic_club_default",
            ),
            models.UniqueConstraint(
                fields=["club", "training_type", "location"],
                condition=models.Q(
                    is_personal_booking_default=True,
                    scope="location",
                    personal_booking_trainer__isnull=True,
                ),
                name="uniq_personal_booking_generic_location_default",
            ),
            models.UniqueConstraint(
                fields=["club", "training_type", "personal_booking_trainer"],
                condition=models.Q(
                    is_personal_booking_default=True,
                    scope="club",
                    personal_booking_trainer__isnull=False,
                ),
                name="uniq_personal_booking_trainer_club_default",
            ),
            models.UniqueConstraint(
                fields=["club", "training_type", "location", "personal_booking_trainer"],
                condition=models.Q(
                    is_personal_booking_default=True,
                    scope="location",
                    personal_booking_trainer__isnull=False,
                ),
                name="uniq_personal_booking_trainer_location_default",
            ),
        ]

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        errors = {}
        if self.club_id and self.training_type_id and self.training_type.club_id != self.club_id:
            errors["training_type"] = "Training type must belong to the same club as tariff."
        if self.club_id and self.location_id and self.location.club_id != self.club_id:
            errors["location"] = "Location must belong to the same club as tariff."
        if self.club_id and self.personal_booking_trainer_id and self.personal_booking_trainer.club_id != self.club_id:
            errors["personal_booking_trainer"] = "Personal booking trainer must belong to the same club as tariff."
        if self.scope == self.Scope.LOCATION and not self.location_id:
            errors["location"] = "Location is required for a location-scoped tariff."
        if self.scope == self.Scope.CLUB and self.location_id:
            errors["location"] = "Club-scoped tariff cannot have a location."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if self._state.adding and self.pk is None:
            return super().save(*args, **kwargs)

        field_to_db_column = {
            "club": "club_id",
            "training_type": "training_type_id",
            "location": "location_id",
            "personal_booking_trainer": "personal_booking_trainer_id",
        }
        using = kwargs.get("using") or router.db_for_write(type(self), instance=self)
        tracked_fields = _TARIFF_REVISION_GUARDED_FIELDS
        db_columns = {
            field_to_db_column.get(field_name, field_name)
            for field_name in tracked_fields
        }
        with transaction.atomic(using=using):
            previous = (
                type(self).objects.unscoped()
                .using(using)
                .select_for_update()
                .filter(pk=self.pk)
                .values(*db_columns)
                .first()
            )
            updates = {}
            if previous is not None:
                update_fields = kwargs.get("update_fields")
                if update_fields is None:
                    _validate_tariff_lifecycle_updates(
                        {
                            field_name: getattr(self, field_name)
                            for field_name in _TARIFF_REVISION_LIFECYCLE_FIELDS
                        }
                    )
                else:
                    _validate_tariff_lifecycle_updates(
                        {
                            field_name: getattr(self, field_name)
                            for field_name in _TARIFF_REVISION_LIFECYCLE_FIELDS
                            if field_name in update_fields
                        }
                    )
                fields_to_check = (
                    tracked_fields
                    if update_fields is None
                    else set(update_fields).intersection(tracked_fields)
                )
                for field_name in fields_to_check:
                    db_column = field_to_db_column.get(field_name, field_name)
                    current_value = getattr(self, db_column)
                    if previous[db_column] != current_value:
                        updates[field_name] = current_value
                if _tariff_edge_update_blocked(
                    club_id=previous["club_id"],
                    tariff_id=self.pk,
                    updates=updates,
                ):
                    raise ValidationError("Tariff contracts sealed by a price revision are immutable.")
            if self._state.adding and previous is None and not kwargs.get("force_update"):
                kwargs = {**kwargs, "force_insert": True}
            return super().save(*args, **kwargs)


class TariffComponentQuerySet(TenantQuerySet):
    def bulk_create(self, objs, **kwargs):
        if kwargs.get("update_conflicts"):
            raise ValidationError(
                "Tariff component upserts are disabled while tariff revisions are supported."
            )
        objects = list(objs)
        if not objects:
            return super().bulk_create(objects, **kwargs)
        tariff_ids = [
            _coerce_component_tariff_id(getattr(component, "tariff_id", None))
            for component in objects
        ]
        using = self.db
        with transaction.atomic(using=using):
            links = _lock_tariff_bulk_links(using=using, objects=objects)
            parent_clubs = _lock_component_parent_tariffs(using=using, tariff_ids=tariff_ids)
            _validate_component_bulk_objects(
                objects=objects,
                parent_clubs=parent_clubs,
                links=links,
            )
            if any(
                _has_tariff_revision_edge(
                    club_id=parent_clubs.get(tariff_id),
                    tariff_id=tariff_id,
                )
                for tariff_id in tariff_ids
                if tariff_id is not None
            ):
                raise ValidationError("Tariff components sealed by a price revision are immutable.")
            return super().bulk_create(objects, **kwargs)

    def update(self, **kwargs):
        if not _TARIFF_COMPONENT_REVISION_SEALED_FIELDS.intersection(kwargs):
            return super().update(**kwargs)

        using = self.db
        with transaction.atomic(using=using):
            component_ids = list(self.values_list("id", flat=True))
            component_probes = list(
                self.model._base_manager.using(using)
                .filter(id__in=component_ids)
                .values("id", "tariff_id", "club_id")
            )
            requested_tariff_id = None
            if "tariff_id" in kwargs:
                requested_tariff_id = _coerce_component_tariff_id(kwargs["tariff_id"])
            elif "tariff" in kwargs:
                requested_tariff_id = _coerce_component_tariff_id(
                    getattr(kwargs["tariff"], "pk", kwargs["tariff"])
                )
            parent_clubs = _lock_component_parent_tariffs(
                using=using,
                tariff_ids=[
                    row["tariff_id"] for row in component_probes
                ] + ([requested_tariff_id] if requested_tariff_id is not None else []),
            )
            component_rows = list(
                self.model._base_manager.using(using)
                .select_for_update()
                .filter(id__in=component_ids)
                .values("id", "tariff_id", "club_id")
            )
            if any(
                _has_tariff_revision_edge(club_id=row["club_id"], tariff_id=row["tariff_id"])
                or (
                    requested_tariff_id is not None
                    and _has_tariff_revision_edge(
                        club_id=parent_clubs.get(requested_tariff_id),
                        tariff_id=requested_tariff_id,
                    )
                )
                for row in component_rows
            ):
                raise ValidationError("Tariff components sealed by a price revision are immutable.")
            return super().update(**kwargs)


class TariffComponentManager(TenantManager):
    def get_queryset(self):
        return TariffComponentQuerySet(self.model, using=self._db)

    def unscoped(self):
        return self.get_queryset()


class TariffComponent(TenantMixin):
    class EntitlementKind(models.TextChoices):
        FINITE_CREDITS = "finite_credits", "Finite credits"
        WEEKLY_LIMIT = "weekly_limit", "Weekly limit"
        UNLIMITED = "unlimited", "Unlimited"

    tariff = models.ForeignKey(
        Tariff,
        on_delete=models.PROTECT,
        related_name="components",
    )
    name = models.CharField(max_length=200, blank=True, default="")
    training_type = models.ForeignKey(
        TrainingType,
        on_delete=models.PROTECT,
        related_name="tariff_components",
    )
    entitlement_kind = models.CharField(
        max_length=20,
        choices=EntitlementKind.choices,
        default=EntitlementKind.FINITE_CREDITS,
    )
    credits_total = models.PositiveIntegerField(null=True, blank=True)
    weekly_limit = models.PositiveIntegerField(null=True, blank=True)
    scope = models.CharField(max_length=20, choices=Tariff.Scope.choices, default=Tariff.Scope.CLUB)
    location = models.ForeignKey(
        "clubs.Location",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="tariff_components",
    )
    trainer_payout_policy = models.CharField(
        max_length=20,
        choices=Tariff.PayoutPolicy.choices,
        default=Tariff.PayoutPolicy.NONE,
    )
    paid_amount_basis = models.DecimalField(max_digits=10, decimal_places=2)
    sort_order = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    objects = TariffComponentManager()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(paid_amount_basis__gt=0),
                name="billing_tariffcomp_paid_basis_positive",
            ),
            models.CheckConstraint(
                condition=(~models.Q(entitlement_kind="finite_credits") | models.Q(credits_total__gt=0)),
                name="billing_tcomp_finite_credits_required",
            ),
            models.CheckConstraint(
                condition=(~models.Q(entitlement_kind="weekly_limit") | models.Q(weekly_limit__gt=0)),
                name="billing_tcomp_weekly_limit_required",
            ),
            models.CheckConstraint(
                condition=(~models.Q(scope=Tariff.Scope.LOCATION) | models.Q(location__isnull=False)),
                name="billing_tcomp_location_scope_required",
            ),
        ]
        indexes = [
            models.Index(fields=["club", "tariff", "is_active"], name="billing_tcomp_tariff_idx"),
            models.Index(fields=["club", "training_type"], name="billing_tcomp_type_idx"),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.club_id and self.tariff_id and self.tariff.club_id != self.club_id:
            errors["tariff"] = "Tariff must belong to the same club as component."
        if self.club_id and self.training_type_id and self.training_type.club_id != self.club_id:
            errors["training_type"] = "Training type must belong to the same club as component."
        if self.club_id and self.location_id and self.location.club_id != self.club_id:
            errors["location"] = "Location must belong to the same club as component."
        if self.entitlement_kind == self.EntitlementKind.FINITE_CREDITS and not self.credits_total:
            errors["credits_total"] = "Credits are required for finite-credit components."
        if self.entitlement_kind == self.EntitlementKind.WEEKLY_LIMIT and not self.weekly_limit:
            errors["weekly_limit"] = "Weekly limit is required for weekly-limit components."
        if self.scope == Tariff.Scope.LOCATION and not self.location_id:
            errors["location"] = "Location is required for location-scoped components."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if self._state.adding and self.pk is None:
            using = kwargs.get("using") or router.db_for_write(type(self), instance=self)
            target_tariff_id = _coerce_component_tariff_id(self.tariff_id)
            with transaction.atomic(using=using):
                parent_clubs = _lock_component_parent_tariffs(
                    using=using,
                    tariff_ids=[target_tariff_id],
                )
                if _has_tariff_revision_edge(
                    club_id=parent_clubs.get(target_tariff_id),
                    tariff_id=target_tariff_id,
                ):
                    raise ValidationError("Tariff components sealed by a price revision are immutable.")
                if self._state.adding and not kwargs.get("force_update"):
                    kwargs = {**kwargs, "force_insert": True}
                return super().save(*args, **kwargs)

        field_to_db_column = {
            "club": "club_id",
            "tariff": "tariff_id",
            "training_type": "training_type_id",
            "location": "location_id",
        }
        using = kwargs.get("using") or router.db_for_write(type(self), instance=self)
        with transaction.atomic(using=using):
            previous_probe = (
                type(self).objects.unscoped()
                .using(using)
                .filter(pk=self.pk)
                .values("club_id", "tariff_id")
                .first()
            )
            if previous_probe is None:
                target_tariff_id = _coerce_component_tariff_id(self.tariff_id)
                parent_clubs = _lock_component_parent_tariffs(
                    using=using,
                    tariff_ids=[target_tariff_id],
                )
                if _has_tariff_revision_edge(
                    club_id=parent_clubs.get(target_tariff_id),
                    tariff_id=target_tariff_id,
                ):
                    raise ValidationError("Tariff components sealed by a price revision are immutable.")
                if self._state.adding and not kwargs.get("force_update"):
                    kwargs = {**kwargs, "force_insert": True}
                return super().save(*args, **kwargs)
            target_tariff_id = _coerce_component_tariff_id(self.tariff_id)
            parent_clubs = _lock_component_parent_tariffs(
                using=using,
                tariff_ids=[previous_probe["tariff_id"], target_tariff_id],
            )
            previous = (
                type(self).objects.unscoped()
                .using(using)
                .select_for_update()
                .filter(pk=self.pk)
                .values(
                    "club_id",
                    "tariff_id",
                    "name",
                    "training_type_id",
                    "entitlement_kind",
                    "credits_total",
                    "weekly_limit",
                    "scope",
                    "location_id",
                    "trainer_payout_policy",
                    "paid_amount_basis",
                    "sort_order",
                    "is_active",
                )
                .first()
            )
            if previous is not None:
                if previous["tariff_id"] != previous_probe["tariff_id"]:
                    raise ValidationError("Tariff component changed while it was being edited.")
                update_fields = kwargs.get("update_fields")
                fields_to_check = (
                    _TARIFF_COMPONENT_REVISION_SEALED_FIELDS
                    if update_fields is None
                    else set(update_fields).intersection(_TARIFF_COMPONENT_REVISION_SEALED_FIELDS)
                )
                updates = {}
                for field_name in fields_to_check:
                    db_column = field_to_db_column.get(field_name, field_name)
                    current_value = getattr(self, db_column)
                    if previous[db_column] != current_value:
                        updates[field_name] = current_value
                original_parent_sealed = _has_tariff_revision_edge(
                    club_id=previous["club_id"],
                    tariff_id=previous["tariff_id"],
                )
                target_parent_sealed = _has_tariff_revision_edge(
                    club_id=parent_clubs.get(target_tariff_id),
                    tariff_id=target_tariff_id,
                )
                if (original_parent_sealed or target_parent_sealed) and updates:
                    raise ValidationError("Tariff components sealed by a price revision are immutable.")
            return super().save(*args, **kwargs)

    def __str__(self):
        return self.name or f"{self.tariff} / {self.training_type}"


class TariffPriceRevisionQuerySet(TenantQuerySet):
    """Price-revision edges are durable audit evidence, never mutable state."""

    def bulk_create(self, objs, **kwargs):
        if kwargs.get("update_conflicts"):
            raise ValidationError("Tariff price revisions are append-only.")
        objects = list(objs)
        for obj in objects:
            if not obj._state.adding:
                raise ValidationError("Tariff price revisions are append-only.")
            obj.full_clean()
        return super().bulk_create(objects, **kwargs)

    def update(self, **kwargs):
        raise ValidationError("Tariff price revisions are append-only.")

    def delete(self):
        raise ValidationError("Tariff price revisions are append-only.")


class TariffPriceRevisionManager(TenantManager):
    def get_queryset(self):
        return TariffPriceRevisionQuerySet(self.model, using=self._db)

    def unscoped(self):
        return self.get_queryset()


class TariffPriceRevision(TenantMixin):
    """One immutable, tenant-scoped edge between compatible tariff versions."""

    source_tariff = models.ForeignKey(
        Tariff,
        on_delete=models.PROTECT,
        related_name="price_revision_sources",
    )
    target_tariff = models.OneToOneField(
        Tariff,
        on_delete=models.PROTECT,
        related_name="price_revision_target",
    )
    source_component = models.ForeignKey(
        TariffComponent,
        on_delete=models.PROTECT,
        related_name="price_revision_sources",
    )
    target_component = models.OneToOneField(
        TariffComponent,
        on_delete=models.PROTECT,
        related_name="price_revision_target",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="tariff_price_revisions",
    )
    idempotency_key = models.CharField(max_length=120)
    payload_fingerprint = models.CharField(max_length=64)
    source_contract = models.JSONField(default=dict)
    target_contract = models.JSONField(default=dict)
    compatibility_snapshot = models.JSONField(default=dict)
    component_mapping = models.JSONField(default=dict)

    objects = TariffPriceRevisionManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["club", "idempotency_key"],
                name="uniq_tariff_price_revision_key",
            ),
            models.UniqueConstraint(
                fields=["club", "source_tariff"],
                name="uniq_tariff_price_revision_source",
            ),
            models.UniqueConstraint(
                fields=["club", "target_tariff"],
                name="uniq_tariff_price_revision_target",
            ),
            models.CheckConstraint(
                condition=~models.Q(source_tariff=models.F("target_tariff")),
                name="billing_tariff_revision_distinct_tariffs",
            ),
        ]
        indexes = [
            models.Index(
                fields=["club", "source_tariff"],
                name="billing_tariff_rev_src_idx",
            ),
            models.Index(
                fields=["club", "target_tariff"],
                name="billing_tariff_rev_tgt_idx",
            ),
        ]

    @property
    def source(self):
        return self.source_tariff

    @property
    def target(self):
        return self.target_tariff

    @property
    def mapping_evidence(self):
        return self.component_mapping

    def clean(self):
        super().clean()
        errors = {}
        for field_name in ("source_tariff", "target_tariff", "source_component", "target_component"):
            value = getattr(self, field_name, None)
            if value is not None and value.club_id != self.club_id:
                errors[field_name] = "Revision records must belong to the same club."
        if self.source_tariff_id and self.target_tariff_id and self.source_tariff_id == self.target_tariff_id:
            errors["target_tariff"] = "A tariff revision must point to a new tariff version."
        if self.source_component_id and self.source_component.tariff_id != self.source_tariff_id:
            errors["source_component"] = "Source component must belong to the source tariff."
        if self.target_component_id and self.target_component.tariff_id != self.target_tariff_id:
            errors["target_component"] = "Target component must belong to the target tariff."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Tariff price revisions are append-only.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Tariff price revisions are append-only.")


class Subscription(TenantMixin, SoftDeleteMixin):
    class Status(models.TextChoices):
        ACTIVE = "active", "Активен"
        EXPIRED = "expired", "Истёк"
        PENDING = "pending", "Ожидает"
        FROZEN = "frozen", "Заморожен"
        CANCELLED = "cancelled", "Отменён"

    student = models.ForeignKey(
        "students.Student",
        on_delete=models.PROTECT,
        related_name="subscriptions",
    )
    tariff = models.ForeignKey(Tariff, on_delete=models.PROTECT, related_name="subscriptions")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    paid_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    trainings_left = models.PositiveIntegerField(null=True)
    trainings_used = models.PositiveIntegerField(default=0)
    activated_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    scope = models.CharField(max_length=20)
    location = models.ForeignKey(
        "clubs.Location",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="subscriptions",
    )
    trainer_payout_policy_snapshot = models.CharField(
        max_length=20,
        choices=Tariff.PayoutPolicy.choices,
        blank=True,
        default="",
    )
    renewed_from = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="renewals",
    )
    renewal_chain_id = models.UUIDField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(paid_amount__isnull=True) | models.Q(paid_amount__gt=0),
                name="billing_subscription_paid_positive",
            ),
            # A source may have historical/terminal renewal attempts, but only
            # one pending financial child may remain actionable at a time.
            models.UniqueConstraint(
                fields=["renewed_from"],
                condition=models.Q(
                    renewed_from__isnull=False,
                    status="pending",
                    deleted_at__isnull=True,
                ),
                name="uniq_live_pending_subscription_renewal",
            ),
        ]
        indexes = [
            models.Index(fields=["club", "student", "status", "expires_at"]),
        ]

    def __str__(self):
        return f"{self.student} - {self.tariff.name}"


class SubscriptionRenewalEventQuerySet(TenantQuerySet):
    """Renewal evidence is append-only, including manager escape hatches."""

    def update(self, **kwargs):
        raise ValidationError("Subscription renewal events are append-only.")

    def delete(self):
        raise ValidationError("Subscription renewal events are append-only.")


class SubscriptionRenewalEventManager(TenantManager):
    def get_queryset(self):
        return SubscriptionRenewalEventQuerySet(self.model, using=self._db)

    def unscoped(self):
        return self.get_queryset()


class SubscriptionRenewalEvent(TenantMixin):
    """One immutable receipt for a finalized renewal child subscription."""

    renewed_from = models.ForeignKey(
        Subscription,
        on_delete=models.PROTECT,
        related_name="renewal_finalization_events",
    )
    renewed_to = models.OneToOneField(
        Subscription,
        on_delete=models.PROTECT,
        related_name="renewal_finalization_event",
    )
    payment = models.ForeignKey(
        "billing.Payment",
        on_delete=models.PROTECT,
        related_name="subscription_renewal_events",
    )
    finalized_at = models.DateTimeField()
    carry_snapshot = models.JSONField(default=dict, blank=True)

    objects = SubscriptionRenewalEventManager()

    class Meta:
        indexes = [
            models.Index(fields=["club", "renewed_from", "created_at"], name="bill_renew_src_idx"),
            models.Index(fields=["club", "payment", "created_at"], name="bill_renew_pay_idx"),
        ]

    def clean(self):
        super().clean()
        errors = {}
        for field_name in ("renewed_from", "renewed_to"):
            subscription = getattr(self, field_name, None)
            if self.club_id and subscription is not None and subscription.club_id != self.club_id:
                errors[field_name] = "Subscription must belong to the same club as renewal event."
        if self.club_id and self.payment_id and self.payment.club_id != self.club_id:
            errors["payment"] = "Payment must belong to the same club as renewal event."
        if self.renewed_to_id and self.renewed_to.renewed_from_id != self.renewed_from_id:
            errors["renewed_to"] = "Renewal event must match the child subscription source."
        if self.payment_id and self.payment.subscription_id != self.renewed_to_id:
            errors["payment"] = "Renewal event payment must own the renewed subscription."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Subscription renewal events are append-only.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Subscription renewal events are append-only.")


class SubscriptionComponent(TenantMixin):
    subscription = models.ForeignKey(
        Subscription,
        on_delete=models.PROTECT,
        related_name="components",
    )
    tariff_component = models.ForeignKey(
        TariffComponent,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="subscription_components",
    )
    name_snapshot = models.CharField(max_length=200, blank=True, default="")
    training_type = models.ForeignKey(
        TrainingType,
        on_delete=models.PROTECT,
        related_name="subscription_components",
    )
    entitlement_kind = models.CharField(
        max_length=20,
        choices=TariffComponent.EntitlementKind.choices,
        default=TariffComponent.EntitlementKind.FINITE_CREDITS,
    )
    credits_total = models.PositiveIntegerField(null=True, blank=True)
    credits_left = models.PositiveIntegerField(null=True, blank=True)
    credits_used = models.PositiveIntegerField(default=0)
    weekly_limit = models.PositiveIntegerField(null=True, blank=True)
    scope = models.CharField(max_length=20, choices=Tariff.Scope.choices, default=Tariff.Scope.CLUB)
    location = models.ForeignKey(
        "clubs.Location",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="subscription_components",
    )
    trainer_payout_policy_snapshot = models.CharField(
        max_length=20,
        choices=Tariff.PayoutPolicy.choices,
        default=Tariff.PayoutPolicy.NONE,
    )
    paid_amount_basis_snapshot = models.DecimalField(max_digits=10, decimal_places=2)
    unit_amount_basis_snapshot = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    sale_trainer_id_snapshot = models.PositiveBigIntegerField(null=True, blank=True)
    sale_rate_percent_snapshot = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    sale_snapshot_provenance = models.CharField(max_length=40, blank=True, default="")
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(paid_amount_basis_snapshot__gt=0),
                name="billing_subcomp_paid_basis_positive",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(entitlement_kind=TariffComponent.EntitlementKind.FINITE_CREDITS)
                    | (models.Q(credits_total__gt=0) & models.Q(credits_left__isnull=False))
                ),
                name="billing_scomp_finite_credits_required",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(entitlement_kind=TariffComponent.EntitlementKind.WEEKLY_LIMIT)
                    | models.Q(weekly_limit__gt=0)
                ),
                name="billing_scomp_weekly_limit_required",
            ),
            models.CheckConstraint(
                condition=(~models.Q(scope=Tariff.Scope.LOCATION) | models.Q(location__isnull=False)),
                name="billing_scomp_location_scope_required",
            ),
            models.UniqueConstraint(
                fields=["subscription", "tariff_component"],
                condition=models.Q(tariff_component__isnull=False),
                name="unique_subscription_tariff_component",
            ),
        ]
        indexes = [
            models.Index(fields=["club", "subscription", "is_active"], name="billing_scomp_sub_idx"),
            models.Index(fields=["club", "training_type", "is_active"], name="billing_scomp_type_idx"),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.club_id and self.subscription_id and self.subscription.club_id != self.club_id:
            errors["subscription"] = "Subscription must belong to the same club as component."
        if self.club_id and self.tariff_component_id and self.tariff_component.club_id != self.club_id:
            errors["tariff_component"] = "Tariff component must belong to the same club as subscription component."
        if self.club_id and self.training_type_id and self.training_type.club_id != self.club_id:
            errors["training_type"] = "Training type must belong to the same club as component."
        if self.club_id and self.location_id and self.location.club_id != self.club_id:
            errors["location"] = "Location must belong to the same club as component."
        if self.entitlement_kind == TariffComponent.EntitlementKind.FINITE_CREDITS and self.credits_total is None:
            errors["credits_total"] = "Credits are required for finite-credit components."
        if self.entitlement_kind == TariffComponent.EntitlementKind.WEEKLY_LIMIT and self.weekly_limit is None:
            errors["weekly_limit"] = "Weekly limit is required for weekly-limit components."
        if self.scope == Tariff.Scope.LOCATION and not self.location_id:
            errors["location"] = "Location is required for location-scoped components."
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"Subscription component #{self.id}: subscription {self.subscription_id}"


class SubscriptionCorrectionQuerySet(TenantQuerySet):
    def update(self, **kwargs):
        raise ValidationError("Subscription corrections are append-only.")

    def delete(self):
        raise ValidationError("Subscription corrections are append-only.")


class SubscriptionCorrectionManager(TenantManager):
    def get_queryset(self):
        return SubscriptionCorrectionQuerySet(self.model, using=self._db)

    def unscoped(self):
        return self.get_queryset()


class SubscriptionCorrection(TenantMixin):
    subscription = models.ForeignKey(Subscription, on_delete=models.PROTECT, related_name="corrections")
    component = models.ForeignKey(
        SubscriptionComponent,
        on_delete=models.PROTECT,
        related_name="corrections",
        null=True,
        blank=True,
    )
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    channel = models.CharField(max_length=30)
    reason = models.TextField()
    command_key = models.CharField(max_length=120)
    payload_fingerprint = models.CharField(max_length=64)
    expected_fingerprint = models.CharField(max_length=64)
    balance_delta = models.IntegerField(default=0)
    before = models.JSONField()
    after = models.JSONField()
    reverses = models.OneToOneField(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="compensation",
    )

    objects = SubscriptionCorrectionManager()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["club", "command_key"], name="bill_correction_command_uniq")]
        indexes = [models.Index(fields=["club", "subscription", "created_at"], name="bill_correction_history_idx")]

    def clean(self):
        super().clean()
        if self.subscription_id and self.subscription.club_id != self.club_id:
            raise ValidationError("Subscription belongs to another club.")
        if self.component_id and (
            self.component.club_id != self.club_id or self.component.subscription_id != self.subscription_id
        ):
            raise ValidationError("Component must belong to the corrected subscription.")
        if self.reverses_id and (
            self.reverses.club_id != self.club_id
            or self.reverses.subscription_id != self.subscription_id
            or self.reverses.component_id != self.component_id
        ):
            raise ValidationError("Compensation must target the original correction.")

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Subscription corrections are append-only.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Subscription corrections are append-only.")


class OpeningEntitlementSnapshotQuerySet(TenantQuerySet):
    def update(self, **kwargs):
        raise ValidationError("Opening entitlement evidence is immutable.")

    def delete(self):
        raise ValidationError("Opening entitlement evidence is immutable.")


class OpeningEntitlementSnapshotManager(TenantManager):
    def get_queryset(self):
        return OpeningEntitlementSnapshotQuerySet(self.model, using=self._db)

    def unscoped(self):
        return self.get_queryset()


class OpeningEntitlementSnapshot(TenantMixin):
    """Reviewed source facts and successful per-entitlement command receipt.

    Mutable remaining credits live on the component. These original facts must
    survive consumption, expiry, cancellation and later financial corrections.
    """

    subscription = models.OneToOneField(
        Subscription,
        on_delete=models.PROTECT,
        related_name="opening_snapshot",
    )
    component = models.OneToOneField(
        SubscriptionComponent,
        on_delete=models.PROTECT,
        related_name="opening_snapshot",
    )
    payment = models.OneToOneField(
        "billing.Payment",
        on_delete=models.PROTECT,
        related_name="opening_snapshot",
    )
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    source_namespace = models.CharField(max_length=120)
    student_source_key = models.CharField(max_length=120)
    entitlement_source_key = models.CharField(max_length=120)
    payload_fingerprint = models.CharField(max_length=64)
    channel = models.CharField(max_length=20)
    started_on = models.DateField()
    expires_on = models.DateField()
    covered_through = models.DateTimeField()
    operational_cutover = models.DateTimeField()
    original_total = models.PositiveIntegerField()
    original_used = models.PositiveIntegerField()
    original_left = models.PositiveIntegerField()
    history_only = models.BooleanField()
    reviewed_input = models.JSONField()
    student_transition = models.JSONField()

    objects = OpeningEntitlementSnapshotManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["club", "source_namespace", "entitlement_source_key"],
                name="uniq_opening_entitlement_source",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(original_total__gt=0)
                    & models.Q(original_total=models.F("original_used") + models.F("original_left"))
                ),
                name="opening_original_credits_balance",
            ),
            models.CheckConstraint(
                condition=models.Q(operational_cutover__gt=models.F("covered_through")),
                name="opening_cutover_after_coverage",
            ),
            models.CheckConstraint(
                condition=models.Q(expires_on__gte=models.F("started_on")),
                name="opening_source_dates_ordered",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        for name in ("subscription", "component", "payment"):
            value = getattr(self, name, None)
            if value is not None and value.club_id != self.club_id:
                errors[name] = "Opening evidence must belong to the same club."
        if self.component_id and self.component.subscription_id != self.subscription_id:
            errors["component"] = "Opening component must belong to the subscription."
        if self.payment_id and (
            self.payment.subscription_id != self.subscription_id
            or self.payment.origin != Payment.Origin.OPENING
            or self.payment.opening_source_namespace != self.source_namespace
        ):
            errors["payment"] = "Opening payment must match the subscription and source."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Opening entitlement evidence is immutable.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Opening entitlement evidence is immutable.")


class Debt(TenantMixin):
    student = models.ForeignKey(
        "students.Student",
        on_delete=models.PROTECT,
        related_name="debts",
    )
    checkin = models.OneToOneField(
        "attendance.Checkin",
        on_delete=models.PROTECT,
    )
    tariff_price = models.DecimalField(max_digits=10, decimal_places=2, null=True)
    reason = models.CharField(max_length=50)
    required_tariff = models.ForeignKey(
        Tariff,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="required_for_debts",
    )
    settlement_payment = models.ForeignKey(
        "billing.Payment",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="settled_debts",
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution_type = models.CharField(max_length=20, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(tariff_price__isnull=True) | models.Q(tariff_price__gte=0),
                name="billing_debt_tariff_price_nonnegative",
            ),
        ]
        indexes = [
            models.Index(fields=["club", "resolved_at"]),
            models.Index(fields=["club", "student"]),
            models.Index(fields=["club", "settlement_payment"], name="billing_deb_club_id_d97685_idx"),
        ]

    def __str__(self):
        return f"Debt: {self.student} - {self.reason}"

    def clean(self):
        super().clean()
        if self.club_id and self.required_tariff_id and self.required_tariff.club_id != self.club_id:
            raise ValidationError({"required_tariff": "Tariff must belong to the same club as debt."})


class DebtSettlementEvent(TenantMixin):
    class EventType(models.TextChoices):
        RESERVED = "reserved", "Reserved"
        CONFIRMED = "confirmed", "Confirmed"
        REJECTED = "rejected", "Rejected"

    debt = models.ForeignKey(
        Debt,
        on_delete=models.PROTECT,
        related_name="settlement_events",
    )
    payment = models.ForeignKey(
        "billing.Payment",
        on_delete=models.PROTECT,
        related_name="debt_settlement_events",
    )
    event_type = models.CharField(max_length=20, choices=EventType.choices)

    class Meta:
        indexes = [
            models.Index(fields=["club", "payment", "event_type", "created_at"]),
            models.Index(fields=["club", "debt", "created_at"]),
        ]

    def clean(self):
        super().clean()
        if self.club_id and self.debt_id and self.debt.club_id != self.club_id:
            raise ValidationError({"debt": "Debt must belong to the same club as event."})
        if self.club_id and self.payment_id and self.payment.club_id != self.club_id:
            raise ValidationError({"payment": "Payment must belong to the same club as event."})

    def __str__(self):
        return f"Debt settlement event #{self.id}: {self.event_type}"


class DebtLifecycleEvent(TenantMixin):
    class EventType(models.TextChoices):
        RESERVED = "reserved", "Reserved"
        CONFIRMED = "confirmed", "Confirmed"
        REJECTED = "rejected", "Rejected"
        ATTACHED = "attached", "Attached"
        CANCELLED = "cancelled", "Cancelled"
        WRITTEN_OFF = "written_off", "Written off"

    debt = models.ForeignKey(
        Debt,
        on_delete=models.PROTECT,
        related_name="lifecycle_events",
    )
    payment = models.ForeignKey(
        "billing.Payment",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="debt_lifecycle_events",
    )
    subscription = models.ForeignKey(
        Subscription,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="debt_lifecycle_events",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="debt_lifecycle_events",
    )
    event_type = models.CharField(max_length=20, choices=EventType.choices)
    reason = models.TextField(blank=True, default="")
    previous_state = models.CharField(max_length=50)
    new_state = models.CharField(max_length=50)
    amount_snapshot = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    debt_id_snapshot = models.PositiveBigIntegerField()
    student_id_snapshot = models.PositiveBigIntegerField()
    student_name_snapshot = models.CharField(max_length=255)
    checkin_id_snapshot = models.PositiveBigIntegerField()
    debt_reason_snapshot = models.CharField(max_length=50)

    class Meta:
        indexes = [
            models.Index(fields=["club", "debt", "created_at"], name="billing_deblife_debt_idx"),
            models.Index(fields=["club", "event_type", "created_at"], name="billing_deblife_type_idx"),
            models.Index(fields=["club", "actor", "created_at"], name="billing_deblife_actor_idx"),
        ]

    def clean(self):
        super().clean()
        if self.club_id and self.debt_id and self.debt.club_id != self.club_id:
            raise ValidationError({"debt": "Debt must belong to the same club as lifecycle event."})
        if self.club_id and self.payment_id and self.payment.club_id != self.club_id:
            raise ValidationError({"payment": "Payment must belong to the same club as lifecycle event."})
        if self.club_id and self.subscription_id and self.subscription.club_id != self.club_id:
            raise ValidationError({"subscription": "Subscription must belong to the same club as lifecycle event."})

    def __str__(self):
        return f"Debt lifecycle event #{self.id}: {self.event_type}"


class DebtWriteOffEvent(TenantMixin):
    debt = models.ForeignKey(
        Debt,
        on_delete=models.PROTECT,
        related_name="writeoff_events",
    )
    written_off_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="debt_writeoff_events",
    )
    reason = models.TextField(blank=True, default="")
    decided_at = models.DateTimeField()
    amount_snapshot = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    debt_id_snapshot = models.PositiveBigIntegerField()
    student_id_snapshot = models.PositiveBigIntegerField()
    student_name_snapshot = models.CharField(max_length=255)
    checkin_id_snapshot = models.PositiveBigIntegerField()
    debt_reason_snapshot = models.CharField(max_length=50)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["club", "debt"],
                name="unique_debt_writeoff_event_per_debt",
            ),
        ]
        indexes = [
            models.Index(fields=["club", "decided_at"], name="billing_woff_club_decided_idx"),
            models.Index(fields=["club", "written_off_by", "created_at"], name="billing_deb_woff_actor_idx"),
        ]

    def clean(self):
        super().clean()
        if self.club_id and self.debt_id and self.debt.club_id != self.club_id:
            raise ValidationError({"debt": "Debt must belong to the same club as write-off event."})

    def __str__(self):
        return f"Debt write-off event #{self.id}: debt #{self.debt_id_snapshot}"


class Discount(TenantMixin):
    class Type(models.TextChoices):
        PERCENT = "percent", "Percent"
        FIXED = "fixed", "Fixed Amount"

    name = models.CharField(max_length=200)
    discount_type = models.CharField(max_length=20, choices=Type.choices)
    value = models.DecimalField(max_digits=10, decimal_places=2)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(discount_type="percent", value__gte=0, value__lte=100)
                    | models.Q(discount_type="fixed", value__gte=0)
                ),
                name="billing_discount_value_valid",
            ),
        ]

    def __str__(self):
        return self.name


class SubscriptionFreeze(TenantMixin):
    class FreezeStatus(models.TextChoices):
        PENDING = "pending", "Ожидает подтверждения"
        APPROVED = "approved", "Подтверждена"
        REJECTED = "rejected", "Отклонена"

    class Reason(models.TextChoices):
        VACATION = "vacation", "Vacation"
        INJURY = "injury", "Injury"
        ILLNESS = "illness", "Illness"
        OTHER = "other", "Other"

    subscription = models.ForeignKey(Subscription, on_delete=models.PROTECT, related_name="freezes")
    days = models.PositiveIntegerField()
    reason = models.CharField(max_length=20, choices=Reason.choices)
    status = models.CharField(
        max_length=20,
        choices=FreezeStatus.choices,
        default=FreezeStatus.APPROVED,
    )
    frozen_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="frozen_subscriptions"
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="approved_subscription_freezes",
    )
    rejected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="rejected_subscription_freezes",
    )
    decision_at = models.DateTimeField(null=True, blank=True)
    decision_reason = models.TextField(blank=True, default="")
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField(null=True, blank=True)  # null = still frozen

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(days__gt=0),
                name="billing_subscriptionfreeze_days_positive",
            ),
            models.UniqueConstraint(
                fields=["club", "subscription"],
                condition=models.Q(status="pending"),
                name="uniq_pending_subscription_freeze_per_sub",
            ),
        ]
        indexes = [
            models.Index(fields=["subscription", "starts_at"]),
            models.Index(fields=["club", "status", "created_at"], name="bill_freeze_status_idx"),
        ]

    def __str__(self):
        return f"Freeze: {self.subscription} ({self.days} days)"


PAYMENT_OPENING_IMMUTABLE_FIELDS = (
    "origin",
    "opening_effective_on",
    "opening_source_namespace",
    "opening_source_key",
    "opening_provenance",
)


class PaymentQuerySet(TenantQuerySet):
    def update(self, **kwargs):
        if set(PAYMENT_OPENING_IMMUTABLE_FIELDS) & set(kwargs):
            raise ValidationError("Payment opening provenance is immutable.")
        if {
            "command_idempotency_key",
            "command_fingerprint",
            "renewal_source_tariff_name_snapshot",
        } & set(kwargs):
            raise ValidationError("Unified payment command identity is immutable.")
        return super().update(**kwargs)


class PaymentManager(TenantManager):
    def get_queryset(self):
        return PaymentQuerySet(self.model, using=self._db)

    def unscoped(self):
        return self.get_queryset()


class Payment(TenantMixin, SoftDeleteMixin):
    class Origin(models.TextChoices):
        ORDINARY = "ordinary", "Обычная оплата"
        OPENING = "opening", "Перенесена"

    class Status(models.TextChoices):
        PENDING = "pending", "Ожидает"
        CONFIRMED = "confirmed", "Подтверждён"
        REJECTED = "rejected", "Отклонён"

    class Method(models.TextChoices):
        CASH = "cash", "Наличные"
        TRANSFER = "transfer", "Перевод"
        ONLINE = "online", "Онлайн"
        UNKNOWN = "unknown", "Исторический способ неизвестен"

    class SaleSnapshotProvenance(models.TextChoices):
        OPENING_REVIEWED = "opening_reviewed", "Reviewed opening terms"
        CONFIRM_TIME = "confirm_time", "Confirm time"
        LEGACY_BACKFILL_CURRENT_STATE = (
            "legacy_backfill_current_state",
            "Legacy backfill current state",
        )

    class GroupMembershipActionSnapshot(models.TextChoices):
        NEW_ADMISSION = "new_admission", "New admission"
        RENEWAL = "renewal", "Renewal"

    origin = models.CharField(max_length=20, choices=Origin.choices, default=Origin.ORDINARY)
    opening_effective_on = models.DateField(null=True, blank=True)
    opening_source_namespace = models.CharField(max_length=120, blank=True, default="")
    opening_source_key = models.CharField(max_length=120, blank=True, default="")
    opening_provenance = models.JSONField(default=dict, blank=True)

    student = models.ForeignKey(
        "students.Student",
        on_delete=models.PROTECT,
        related_name="payments",
    )
    tariff = models.ForeignKey(Tariff, on_delete=models.PROTECT, related_name="payments")
    subscription = models.OneToOneField(
        Subscription,
        on_delete=models.PROTECT,
        null=True,
        related_name="payment",
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    original_amount = models.DecimalField(max_digits=10, decimal_places=2)
    payment_method = models.CharField(max_length=20, choices=Method.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    # Unified commercial commands carry a stable client key.  Legacy payment
    # entry points remain deliberately nullable during the rollout window.
    command_idempotency_key = models.CharField(max_length=120, null=True, blank=True)
    command_fingerprint = models.CharField(max_length=64, blank=True, default="")
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="recorded_payments",
    )
    seller_trainer = models.ForeignKey(
        "trainers.Trainer",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="sales",
    )
    package_owner_trainer = models.ForeignKey(
        "trainers.Trainer",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="package_payments",
    )
    target_schedule = models.ForeignKey(
        "attendance.Schedule",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="conversion_payments",
    )
    conversion_enrollment = models.ForeignKey(
        "attendance.ScheduleEnrollment",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="conversion_payments",
    )
    target_training_group = models.ForeignKey(
        "attendance.TrainingGroup",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="target_payments",
    )
    target_group_membership = models.ForeignKey(
        "attendance.TrainingGroupMembership",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="target_payments",
    )
    conversion_group_membership = models.ForeignKey(
        "attendance.TrainingGroupMembership",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="conversion_payments",
    )
    group_membership_action_snapshot = models.CharField(
        max_length=20,
        choices=GroupMembershipActionSnapshot.choices,
        blank=True,
        default="",
    )
    target_start_date = models.DateField(null=True, blank=True)
    target_group_name_snapshot = models.CharField(max_length=100, blank=True, default="")
    renewal_source_tariff_name_snapshot = models.CharField(max_length=200, blank=True, default="")
    target_location_id_snapshot = models.PositiveBigIntegerField(null=True, blank=True)
    target_location_name_snapshot = models.CharField(max_length=200, blank=True, default="")
    target_trainer_id_snapshot = models.PositiveBigIntegerField(null=True, blank=True)
    target_trainer_name_snapshot = models.CharField(max_length=255, blank=True, default="")
    target_training_type_id_snapshot = models.PositiveBigIntegerField(null=True, blank=True)
    target_training_type_kind_snapshot = models.CharField(max_length=20, blank=True, default="")
    sale_attribution_source = models.CharField(max_length=50, blank=True, default="")
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="verified_payments",
    )
    verified_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True)
    sale_earning_snapshot_recorded = models.BooleanField(default=False)
    sale_trainer_id_snapshot = models.PositiveBigIntegerField(null=True, blank=True)
    sale_training_type_id_snapshot = models.PositiveBigIntegerField(null=True, blank=True)
    sale_training_type_kind_snapshot = models.CharField(max_length=20, blank=True, default="")
    sale_rate_percent_snapshot = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    sale_amount_basis_snapshot = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    sale_snapshot_provenance = models.CharField(
        max_length=40,
        choices=SaleSnapshotProvenance.choices,
        blank=True,
        default="",
    )
    applied_discounts = models.ManyToManyField("Discount", blank=True, related_name="payments")

    objects = PaymentManager()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(
                        origin="ordinary",
                        opening_effective_on__isnull=True,
                        opening_source_namespace="",
                        opening_source_key="",
                        opening_provenance={},
                    )
                    | (
                        models.Q(
                            origin="opening",
                            opening_effective_on__isnull=False,
                            status="confirmed",
                            verified_at__isnull=False,
                        )
                        & ~models.Q(opening_source_namespace="")
                        & ~models.Q(opening_source_key="")
                    )
                ),
                name="billing_payment_origin_terms",
            ),
            models.CheckConstraint(
                condition=~models.Q(payment_method="unknown") | models.Q(origin="opening"),
                name="billing_payment_unknown_opening_only",
            ),
            models.UniqueConstraint(
                fields=["club", "opening_source_namespace", "opening_source_key"],
                condition=models.Q(origin="opening"),
                name="uniq_opening_payment_source",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0),
                name="billing_payment_amount_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(original_amount__gt=0),
                name="billing_payment_original_positive",
            ),
            models.UniqueConstraint(
                fields=["conversion_group_membership"],
                condition=models.Q(conversion_group_membership__isnull=False),
                name="uniq_payment_conversion_group_membership",
            ),
            models.UniqueConstraint(
                fields=["club", "command_idempotency_key"],
                condition=models.Q(command_idempotency_key__isnull=False),
                name="uniq_payment_command_idempotency",
            ),
        ]
        indexes = [
            models.Index(fields=["club", "student", "status"]),
            models.Index(fields=["club", "status", "created_at"]),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.club_id and self.target_schedule_id and self.target_schedule.club_id != self.club_id:
            errors["target_schedule"] = "Target schedule must belong to the same club as payment."
        if self.club_id and self.conversion_enrollment_id and self.conversion_enrollment.club_id != self.club_id:
            errors["conversion_enrollment"] = "Conversion enrollment must belong to the same club as payment."
        if self.club_id and self.target_training_group_id:
            group = self.target_training_group
            if group.club_id != self.club_id:
                errors["target_training_group"] = "Target training group must belong to the same club as payment."
            if self.tariff_id and group.training_type_id != self.tariff.training_type_id:
                errors["target_training_group"] = "Target training group must match the tariff training type."
            if self.tariff_id and self.tariff.location_id and group.location_id != self.tariff.location_id:
                errors["target_training_group"] = "Target training group must match the tariff location."
            if self.target_schedule_id and self.target_schedule.training_group_id != group.id:
                errors["target_schedule"] = "Target schedule must belong to the target training group."
        for field_name in ("target_group_membership", "conversion_group_membership"):
            membership = getattr(self, field_name)
            if membership is None:
                continue
            if self.club_id and membership.club_id != self.club_id:
                errors[field_name] = "Group membership must belong to the same club as payment."
            elif self.student_id and membership.student_id != self.student_id:
                errors[field_name] = "Group membership must belong to the payment student."
            elif self.target_training_group_id and membership.training_group_id != self.target_training_group_id:
                errors[field_name] = "Group membership must match the target training group."
        if self.group_membership_action_snapshot == self.GroupMembershipActionSnapshot.NEW_ADMISSION:
            if self.conversion_group_membership_id and (
                self.target_group_membership_id != self.conversion_group_membership_id
            ):
                errors["conversion_group_membership"] = "New admission must target its payment-owned membership."
            if self.target_start_date and self.conversion_group_membership_id:
                if self.conversion_group_membership.starts_on != self.target_start_date:
                    errors["conversion_group_membership"] = "New admission membership must start on the target date."
        if self.group_membership_action_snapshot == self.GroupMembershipActionSnapshot.RENEWAL:
            if self.conversion_group_membership_id:
                errors["conversion_group_membership"] = "Renewal cannot own a group membership."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            original = type(self).objects.filter(pk=self.pk).values(*PAYMENT_OPENING_IMMUTABLE_FIELDS).first()
            if original and any(original[field] != getattr(self, field) for field in PAYMENT_OPENING_IMMUTABLE_FIELDS):
                raise ValidationError("Payment opening provenance is immutable.")
            previous = (
                type(self)
                .objects.filter(pk=self.pk)
                .values(
                    "target_training_group_id",
                    "target_group_membership_id",
                    "conversion_group_membership_id",
                    "group_membership_action_snapshot",
                    "command_idempotency_key",
                    "command_fingerprint",
                    "renewal_source_tariff_name_snapshot",
                )
                .first()
            )
            if previous:
                immutable_fields = (
                    "target_training_group_id",
                    "target_group_membership_id",
                    "conversion_group_membership_id",
                    "group_membership_action_snapshot",
                    "command_idempotency_key",
                    "command_fingerprint",
                    "renewal_source_tariff_name_snapshot",
                )
                for field_name in immutable_fields:
                    old_value = previous[field_name]
                    new_value = getattr(self, field_name)
                    if old_value not in (None, "") and old_value != new_value:
                        raise ValidationError({field_name: "Payment group ownership is immutable once set."})
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"Payment #{self.id}: {self.student} - {self.amount}"


class BankPaymentOrder(TenantMixin):
    class Provider(models.TextChoices):
        MOCK = "mock", "Mock"
        TOCHKA = "tochka", "Tochka"

    class Source(models.TextChoices):
        TRAINER = "trainer", "Trainer"
        OWNER = "owner", "Owner"
        ADMIN = "admin", "Admin"
        STUDENT = "student", "Student"
        PARENT = "parent", "Parent"

    class Status(models.TextChoices):
        CREATED = "created", "Created"
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        AUTHORIZED = "authorized", "Authorized"
        FAILED = "failed", "Failed"
        EXPIRED = "expired", "Expired"
        CANCELLED = "cancelled", "Cancelled"
        MANUAL_REVIEW = "manual_review", "Manual review"
        REFUNDED = "refunded", "Refunded"
        REFUNDED_PARTIALLY = "refunded_partially", "Refunded partially"

    class ReceiptMode(models.TextChoices):
        NONE = "none", "No receipt"
        TOCHKA_RECEIPT = "tochka_receipt", "Tochka receipt"

    class ReceiptStatus(models.TextChoices):
        NOT_REQUIRED = "not_required", "Not required"
        PENDING = "pending", "Pending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"

    class LinkCreationState(models.TextChoices):
        READY = "ready", "Ready"
        CLAIMED = "claimed", "Claimed"
        DISPATCHED = "dispatched", "Dispatched"
        UNKNOWN = "unknown", "Unknown"

    payment = models.ForeignKey(
        Payment,
        on_delete=models.PROTECT,
        related_name="bank_orders",
    )
    subscription = models.ForeignKey(
        Subscription,
        on_delete=models.PROTECT,
        related_name="bank_orders",
    )
    student = models.ForeignKey(
        "students.Student",
        on_delete=models.PROTECT,
        related_name="bank_payment_orders",
    )
    provider = models.CharField(max_length=20, choices=Provider.choices)
    source = models.CharField(max_length=20, choices=Source.choices)
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.CREATED)
    amount_snapshot = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default="RUB")
    purpose_snapshot = models.CharField(max_length=255)
    provider_operation_id = models.CharField(max_length=120, blank=True, default="")
    provider_payment_link_id = models.CharField(max_length=45, blank=True, default="")
    provider_payment_url = models.URLField(max_length=1000, blank=True, default="")
    provider_payment_modes = models.JSONField(default=list, blank=True)
    provider_status = models.CharField(max_length=60, blank=True, default="")
    provider_customer_code = models.CharField(max_length=120, blank=True, default="")
    provider_merchant_id = models.CharField(max_length=120, blank=True, default="")
    payment_intent_key = models.CharField(max_length=64, blank=True, default="")
    link_creation_state = models.CharField(
        max_length=20,
        choices=LinkCreationState.choices,
        default=LinkCreationState.READY,
    )
    link_creation_claimed_at = models.DateTimeField(null=True, blank=True)
    link_creation_dispatched_at = models.DateTimeField(null=True, blank=True)
    creation_absence_count = models.PositiveSmallIntegerField(default=0)
    creation_last_absence_at = models.DateTimeField(null=True, blank=True)
    creation_recovery_claim_token = models.CharField(max_length=64, blank=True, default="")
    creation_recovery_claimed_at = models.DateTimeField(null=True, blank=True)
    creation_recovery_failure_count = models.PositiveSmallIntegerField(default=0)
    creation_recovery_retry_at = models.DateTimeField(null=True, blank=True)
    personal_booking_reservation_id_snapshot = models.PositiveBigIntegerField(null=True, blank=True)
    personal_drop_in_booking_id_snapshot = models.PositiveBigIntegerField(null=True, blank=True)
    expires_at = models.DateTimeField()
    paid_at = models.DateTimeField(null=True, blank=True)
    receipt_mode = models.CharField(
        max_length=30,
        choices=ReceiptMode.choices,
        default=ReceiptMode.NONE,
    )
    buyer_email = models.EmailField(blank=True, default="")
    buyer_phone = models.CharField(max_length=32, blank=True, default="")
    receipt_status = models.CharField(
        max_length=30,
        choices=ReceiptStatus.choices,
        default=ReceiptStatus.NOT_REQUIRED,
    )
    receipt_provider_id = models.CharField(max_length=120, blank=True, default="")
    receipt_url = models.URLField(max_length=1000, blank=True, default="")
    receipt_error_code = models.CharField(max_length=120, blank=True, default="")
    receipt_error_message = models.TextField(blank=True, default="")
    fiscal_item_snapshot = models.JSONField(default=dict, blank=True)
    renewed_from_subscription = models.ForeignKey(
        Subscription,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="renewal_bank_orders",
    )
    renewal_chain_id = models.UUIDField(null=True, blank=True)
    last_error_code = models.CharField(max_length=120, blank=True, default="")
    last_error_message = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_bank_payment_orders",
    )
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="confirmed_bank_payment_orders",
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount_snapshot__gt=0),
                name="billing_bank_order_amount_positive",
            ),
            models.UniqueConstraint(
                fields=["provider", "provider_payment_link_id"],
                condition=~models.Q(provider_payment_link_id=""),
                name="uniq_bank_order_provider_link_id",
            ),
            models.UniqueConstraint(
                fields=["provider", "provider_operation_id"],
                condition=~models.Q(provider_operation_id=""),
                name="uniq_bank_order_provider_operation",
            ),
            models.UniqueConstraint(
                fields=["club", "payment_intent_key"],
                condition=(
                    ~models.Q(payment_intent_key="")
                    & models.Q(status__in=["created", "pending", "authorized", "manual_review"])
                ),
                name="uniq_bank_order_payment_intent",
            ),
            models.UniqueConstraint(
                fields=["club", "personal_booking_reservation_id_snapshot"],
                condition=(
                    models.Q(personal_booking_reservation_id_snapshot__isnull=False)
                    & models.Q(status__in=["created", "pending", "authorized", "manual_review"])
                ),
                name="uniq_live_bank_order_personal_reservation",
            ),
            models.UniqueConstraint(
                fields=["club", "personal_drop_in_booking_id_snapshot"],
                condition=(
                    models.Q(personal_drop_in_booking_id_snapshot__isnull=False)
                    & models.Q(status__in=["created", "pending", "authorized", "manual_review"])
                ),
                name="uniq_live_bank_order_personal_dropin",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(personal_booking_reservation_id_snapshot__isnull=True)
                    | models.Q(personal_drop_in_booking_id_snapshot__isnull=True)
                ),
                name="bank_order_single_personal_origin",
            ),
        ]
        indexes = [
            models.Index(fields=["club", "status", "created_at"], name="billing_bank_order_status_idx"),
            models.Index(fields=["club", "payment"], name="billing_bank_order_payment_idx"),
            models.Index(fields=["club", "student", "created_at"], name="billing_bank_order_student_idx"),
            models.Index(
                fields=["provider", "link_creation_state", "creation_recovery_retry_at"],
                name="billing_creation_recover_idx",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.club_id and self.payment_id and self.payment.club_id != self.club_id:
            errors["payment"] = "Payment must belong to the same club as bank order."
        if self.club_id and self.subscription_id and self.subscription.club_id != self.club_id:
            errors["subscription"] = "Subscription must belong to the same club as bank order."
        if self.club_id and self.student_id and self.student.club_id != self.club_id:
            errors["student"] = "Student must belong to the same club as bank order."
        if (
            self.club_id
            and self.renewed_from_subscription_id
            and self.renewed_from_subscription.club_id != self.club_id
        ):
            errors["renewed_from_subscription"] = "Renewed subscription must belong to the same club."
        if self.provider_payment_link_id and len(self.provider_payment_link_id) > 45:
            errors["provider_payment_link_id"] = "Provider payment link id must be 45 characters or fewer."
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"Bank order #{self.id}: payment {self.payment_id} ({self.provider})"


class BankPaymentProviderEvent(TenantMixin):
    class ProcessingStatus(models.TextChoices):
        RECEIVED = "received", "Received"
        PROCESSED = "processed", "Processed"
        IGNORED = "ignored", "Ignored"
        FAILED = "failed", "Failed"
        DEFERRED = "deferred", "Deferred"

    order = models.ForeignKey(
        BankPaymentOrder,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="provider_events",
    )
    provider = models.CharField(max_length=20, choices=BankPaymentOrder.Provider.choices)
    event_type = models.CharField(max_length=80)
    provider_event_id = models.CharField(max_length=120, blank=True, default="")
    payload_hash = models.CharField(max_length=64, blank=True, default="")
    provider_operation_id = models.CharField(max_length=120, blank=True, default="")
    provider_payment_link_id = models.CharField(max_length=45, blank=True, default="")
    provider_status = models.CharField(max_length=60, blank=True, default="")
    normalized_status_snapshot = models.CharField(max_length=60, blank=True, default="")
    provider_paid_at_snapshot = models.DateTimeField(null=True, blank=True)
    amount_snapshot = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    received_at = models.DateTimeField()
    processed_at = models.DateTimeField(null=True, blank=True)
    processing_status = models.CharField(
        max_length=20,
        choices=ProcessingStatus.choices,
        default=ProcessingStatus.RECEIVED,
    )
    failure_code = models.CharField(max_length=120, blank=True, default="")
    failure_message = models.TextField(blank=True, default="")
    request_id = models.CharField(max_length=120, blank=True, default="")
    redacted_payload_metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "provider_event_id"],
                condition=~models.Q(provider_event_id=""),
                name="uniq_bank_event_provider_event",
            ),
            models.UniqueConstraint(
                fields=["provider", "payload_hash"],
                condition=~models.Q(payload_hash=""),
                name="uniq_bank_event_provider_hash",
            ),
        ]
        indexes = [
            models.Index(fields=["club", "order", "received_at"], name="billing_bank_event_order_idx"),
            models.Index(fields=["club", "processing_status", "received_at"], name="billing_bank_event_status_idx"),
        ]

    def clean(self):
        super().clean()
        if self.club_id and self.order_id and self.order.club_id != self.club_id:
            raise ValidationError({"order": "Order must belong to the same club as provider event."})

    def __str__(self):
        return f"Provider event #{self.id}: {self.provider} {self.provider_status}"


class BankPaymentReconciliationAttempt(TenantMixin):
    """Durable lease for provider-backed payment confirmation.

    The claim is committed before the adapter call.  The caller must then leave
    the transaction, fetch authenticated evidence, and enter a new short
    confirmation transaction.  This prevents bank I/O under financial locks.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        RETRY = "retry", "Retry"
        COMPLETED = "completed", "Completed"
        MANUAL_REVIEW = "manual_review", "Manual review"

    order = models.OneToOneField(
        BankPaymentOrder,
        on_delete=models.PROTECT,
        related_name="reconciliation_attempt",
    )
    provider_event = models.ForeignKey(
        BankPaymentProviderEvent,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reconciliation_attempts",
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    attempt_count = models.PositiveIntegerField(default=0)
    lease_token = models.CharField(max_length=64, blank=True, default="")
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    retry_at = models.DateTimeField(null=True, blank=True)
    last_error_code = models.CharField(max_length=120, blank=True, default="")

    class Meta:
        indexes = [
            models.Index(fields=["club", "status", "retry_at"], name="billing_reconcile_due_idx"),
        ]

    def clean(self):
        super().clean()
        if self.club_id and self.order_id and self.order.club_id != self.club_id:
            raise ValidationError({"order": "Order must belong to the same club as reconciliation attempt."})
        if self.club_id and self.provider_event_id and self.provider_event.club_id != self.club_id:
            raise ValidationError({"provider_event": "Event must belong to the same club as reconciliation attempt."})


class PaymentReturnState(TenantMixin):
    """One-time, non-identifying provider-return bridge."""

    order = models.ForeignKey(BankPaymentOrder, on_delete=models.PROTECT, related_name="return_states")
    state_hash = models.CharField(max_length=64, unique=True)
    purpose = models.CharField(max_length=32, default="payment_return")
    version = models.PositiveSmallIntegerField(default=1)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)
    browser_binding_hash = models.CharField(max_length=64, blank=True, default="")
    session_handle_hash = models.CharField(max_length=64, blank=True, default="")
    session_expires_at = models.DateTimeField(null=True, blank=True)
    terminal_grace_expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["state_hash", "expires_at"], name="billing_return_state_idx"),
            models.Index(
                fields=["session_handle_hash", "session_expires_at"],
                name="billing_return_session_idx",
            ),
        ]


class PaymentProviderReadinessSnapshot(BaseModel):
    """Append-only, redacted evidence from an authenticated provider read-back."""

    provider = models.CharField(max_length=20, choices=BankPaymentOrder.Provider.choices)
    customer_code_hash = models.CharField(max_length=64)
    merchant_id_hash = models.CharField(max_length=64)
    retailer_status = models.CharField(max_length=40)
    is_active = models.BooleanField(default=False)
    payment_modes = models.JSONField(default=list, blank=True)
    cashbox_ready = models.BooleanField(default=False)
    checked_at = models.DateTimeField()
    expires_at = models.DateTimeField()

    class Meta:
        indexes = [
            models.Index(
                fields=["provider", "customer_code_hash", "merchant_id_hash", "-checked_at"],
                name="billing_provider_ready_idx",
            ),
        ]


class ProviderWebhookDelivery(BaseModel):
    """Global, redacted ingress authority for cryptographically verified callbacks."""

    class Outcome(models.TextChoices):
        RECEIVED = "received", "Received"
        MATCHED = "matched", "Matched"
        VERIFIED_NON_ACTIONABLE = "verified_non_actionable", "Verified non-actionable"
        VERIFIED_IDENTIFIER_CONFLICT = "verified_identifier_conflict", "Verified identifier conflict"

    provider = models.CharField(max_length=20, choices=BankPaymentOrder.Provider.choices)
    payload_hash = models.CharField(max_length=64)
    provider_event_id_hash = models.CharField(max_length=64, blank=True, default="")
    event_type = models.CharField(max_length=80, blank=True, default="")
    provider_status = models.CharField(max_length=60, blank=True, default="")
    payment_type = models.CharField(max_length=30, blank=True, default="")
    amount_snapshot = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    received_at = models.DateTimeField()
    processed_at = models.DateTimeField(null=True, blank=True)
    request_id = models.CharField(max_length=120, blank=True, default="")
    outcome = models.CharField(max_length=40, choices=Outcome.choices, default=Outcome.RECEIVED)
    provider_event = models.OneToOneField(
        BankPaymentProviderEvent,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="global_delivery",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "payload_hash"],
                name="uniq_webhook_delivery_provider_payload",
            ),
            models.UniqueConstraint(
                fields=["provider", "provider_event_id_hash"],
                condition=~models.Q(provider_event_id_hash=""),
                name="uniq_webhook_delivery_provider_event",
            ),
        ]
        indexes = [
            models.Index(
                fields=["provider", "outcome", "received_at"],
                name="billing_webhook_delivery_idx",
            ),
        ]

    def __str__(self):
        return f"Webhook delivery #{self.id}: {self.provider} {self.outcome}"


class BankPaymentOrderReviewEvent(TenantMixin):
    class Resolution(models.TextChoices):
        CONFIRM_PAID = "confirm_paid", "Confirm paid"
        REJECT = "reject", "Reject"
        MARK_REFUNDED = "mark_refunded", "Mark refunded"
        MARK_REFUNDED_PARTIALLY = "mark_refunded_partially", "Mark refunded partially"
        RETRY_RECONCILIATION = "retry_reconciliation", "Retry reconciliation"

    order = models.ForeignKey(
        BankPaymentOrder,
        on_delete=models.PROTECT,
        related_name="review_events",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="bank_payment_order_review_events",
    )
    resolution = models.CharField(max_length=40, choices=Resolution.choices)
    previous_status = models.CharField(max_length=30, choices=BankPaymentOrder.Status.choices)
    new_status = models.CharField(max_length=30, choices=BankPaymentOrder.Status.choices)
    previous_payment_status = models.CharField(max_length=20, choices=Payment.Status.choices)
    new_payment_status = models.CharField(max_length=20, choices=Payment.Status.choices)
    previous_subscription_status = models.CharField(
        max_length=20,
        choices=Subscription.Status.choices,
        null=True,
        blank=True,
    )
    new_subscription_status = models.CharField(
        max_length=20,
        choices=Subscription.Status.choices,
        null=True,
        blank=True,
    )
    reason = models.TextField(blank=True, default="")
    evidence_metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["club", "order", "created_at"], name="billing_bank_review_order_idx"),
            models.Index(fields=["club", "resolution", "created_at"], name="billing_bank_review_res_idx"),
            models.Index(fields=["club", "actor", "created_at"], name="billing_bank_review_actor_idx"),
        ]

    def clean(self):
        super().clean()
        if self.club_id and self.order_id and self.order.club_id != self.club_id:
            raise ValidationError({"order": "Order must belong to the same club as review event."})

    def __str__(self):
        return f"Bank order review #{self.id}: order {self.order_id} {self.resolution}"


class PaymentRefundCase(TenantMixin):
    class Kind(models.TextChoices):
        FULL = "full", "Full"
        PARTIAL = "partial", "Partial"

    class Status(models.TextChoices):
        DETECTED = "detected", "Detected"
        RECONCILIATION_REQUIRED = "reconciliation_required", "Reconciliation required"
        RESOLVED = "resolved", "Resolved"

    order = models.ForeignKey(
        BankPaymentOrder,
        on_delete=models.PROTECT,
        related_name="refund_cases",
    )
    provider_event = models.OneToOneField(
        BankPaymentProviderEvent,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="refund_case",
    )
    legacy_review_event = models.OneToOneField(
        BankPaymentOrderReviewEvent,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="refund_case",
    )
    refund_kind = models.CharField(max_length=20, choices=Kind.choices)
    detected_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    provider_refunded_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=40, choices=Status.choices, default=Status.DETECTED)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="resolved_payment_refund_cases",
    )
    resolution_note = models.TextField(blank=True, default="")

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(detected_amount__isnull=True) | models.Q(detected_amount__gt=0),
                name="billing_refund_case_detected_amount_positive",
            ),
        ]
        indexes = [
            models.Index(fields=["club", "status", "created_at"], name="billing_refcase_status_idx"),
            models.Index(fields=["club", "order", "created_at"], name="billing_refcase_order_idx"),
        ]

    def clean(self):
        super().clean()
        errors = {}
        for field_name in ("order", "provider_event", "legacy_review_event"):
            obj = getattr(self, field_name, None)
            if self.club_id and obj is not None and obj.club_id != self.club_id:
                errors[field_name] = "Object must belong to the same club as refund case."
        if self.provider_event_id and self.provider_event.order_id != self.order_id:
            errors["provider_event"] = "Provider event must belong to the refund order."
        if self.legacy_review_event_id and self.legacy_review_event.order_id != self.order_id:
            errors["legacy_review_event"] = "Review event must belong to the refund order."
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"Payment refund case #{self.id}: order {self.order_id}"


class PaymentRefund(TenantMixin):
    class Source(models.TextChoices):
        PROVIDER = "provider", "Provider"
        MANUAL = "manual", "Manual or imported"

    source = models.CharField(max_length=16, choices=Source.choices, default=Source.PROVIDER)
    command_snapshot = models.JSONField(default=dict, blank=True)

    class Kind(models.TextChoices):
        FULL = "full", "Full"
        PARTIAL = "partial", "Partial"

    class Status(models.TextChoices):
        COMPLETED = "completed", "Completed"
        PAYROLL_ACTION_REQUIRED = "payroll_action_required", "Payroll action required"

    class EntitlementDisposition(models.TextChoices):
        KEPT_PARTIAL = "kept_partial", "Kept after partial refund"
        KEEP_CLUB_ABSORBS = "keep_club_absorbs", "Keep; club absorbs"
        REVOKE_REMAINING = "revoke_remaining", "Revoke remaining entitlement"

    class EnrollmentDisposition(models.TextChoices):
        NOT_APPLICABLE = "not_applicable", "Not applicable"
        KEPT = "kept", "Kept"
        CANCELLED_PAYMENT_CREATED = "cancelled_payment_created", "Cancelled payment-created enrollment"
        LEFT_UNLINKED = "left_unlinked", "Legacy enrollment left unlinked"

    class PersonalBookingDisposition(models.TextChoices):
        NOT_APPLICABLE = "not_applicable", "Not applicable"
        KEPT = "kept", "Kept"
        CANCELLED_FUTURE = "cancelled_future", "Cancelled future booking"
        DELIVERED_HISTORY_KEPT = "delivered_history_kept", "Delivered history kept"

    class SettledDebtDisposition(models.TextChoices):
        NONE = "none", "None"
        ABSORBED = "absorbed", "Settlement kept; club absorbs"

    refund_case = models.OneToOneField(
        PaymentRefundCase,
        on_delete=models.PROTECT,
        related_name="refund",
        null=True,
        blank=True,
    )
    order = models.ForeignKey(
        BankPaymentOrder,
        on_delete=models.PROTECT,
        related_name="refunds",
        null=True,
        blank=True,
    )
    payment = models.ForeignKey(
        Payment,
        on_delete=models.PROTECT,
        related_name="refunds",
    )
    subscription = models.ForeignKey(
        Subscription,
        on_delete=models.PROTECT,
        related_name="refunds",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="approved_payment_refunds",
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default="RUB")
    refund_kind = models.CharField(max_length=20, choices=Kind.choices)
    provider_refunded_at = models.DateTimeField(null=True, blank=True)
    accounting_date = models.DateField()
    idempotency_key = models.CharField(max_length=120)
    reason = models.TextField()
    entitlement_disposition = models.CharField(
        max_length=32,
        choices=EntitlementDisposition.choices,
    )
    enrollment_disposition = models.CharField(
        max_length=40,
        choices=EnrollmentDisposition.choices,
        default=EnrollmentDisposition.NOT_APPLICABLE,
    )
    personal_booking_disposition = models.CharField(
        max_length=40,
        choices=PersonalBookingDisposition.choices,
        default=PersonalBookingDisposition.NOT_APPLICABLE,
    )
    settled_debt_disposition = models.CharField(
        max_length=20,
        choices=SettledDebtDisposition.choices,
        default=SettledDebtDisposition.NONE,
    )
    settled_debts_snapshot = models.JSONField(default=list, blank=True)
    entitlement_snapshot = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=40, choices=Status.choices, default=Status.COMPLETED)
    payroll_effective_date = models.DateField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(source="provider", refund_case__isnull=False, order__isnull=False)
                    | models.Q(
                        source="manual", refund_case__isnull=True, order__isnull=True, provider_refunded_at__isnull=True
                    )
                ),
                name="billing_refund_source_links",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0),
                name="billing_payment_refund_amount_positive",
            ),
            models.UniqueConstraint(
                fields=["club", "idempotency_key"],
                name="unique_payment_refund_idempotency",
            ),
        ]
        indexes = [
            models.Index(fields=["club", "accounting_date"], name="billing_refund_date_idx"),
            models.Index(fields=["club", "status", "created_at"], name="billing_refund_status_idx"),
            models.Index(fields=["club", "payment", "created_at"], name="billing_refund_payment_idx"),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.source == self.Source.PROVIDER and (not self.refund_case_id or not self.order_id):
            errors["source"] = "Provider refund requires its case and order."
        elif self.source == self.Source.MANUAL and (self.refund_case_id or self.order_id or self.provider_refunded_at):
            errors["source"] = "Manual refund cannot have provider links."
        if self.payment_id and self.subscription_id and self.payment.subscription_id != self.subscription_id:
            errors["subscription"] = "Subscription must belong to the exact payment."
        for field_name in ("refund_case", "order", "payment", "subscription"):
            obj = getattr(self, field_name, None)
            if self.club_id and obj is not None and obj.club_id != self.club_id:
                errors[field_name] = "Object must belong to the same club as refund."
        if self.order_id and self.payment_id and self.order.payment_id != self.payment_id:
            errors["payment"] = "Payment must belong to the refund order."
        if self.order_id and self.subscription_id and self.order.subscription_id != self.subscription_id:
            errors["subscription"] = "Subscription must belong to the refund order."
        if self.refund_case_id and self.order_id and self.refund_case.order_id != self.order_id:
            errors["refund_case"] = "Refund case must belong to the refund order."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if self.pk and not self._state.adding:
            fields = (
                "source", "command_snapshot", "refund_case_id", "order_id", "payment_id",
                "subscription_id", "club_id", "approved_by_id", "amount", "currency",
                "refund_kind", "provider_refunded_at", "accounting_date", "idempotency_key",
                "reason", "entitlement_disposition", "settled_debt_disposition",
                "settled_debts_snapshot", "entitlement_snapshot",
            )
            previous = type(self).objects.for_club(self.club_id).filter(pk=self.pk).values(*fields).first()
            if previous is None or any(previous[name] != getattr(self, name) for name in fields):
                raise ValidationError("Posted refund financial evidence is immutable.")
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"Payment refund #{self.id}: payment {self.payment_id} ({self.amount})"


class Expense(TenantMixin, SoftDeleteMixin):
    name = models.CharField(max_length=200)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    date = models.DateField()
    category = models.CharField(max_length=100, blank=True, default="")
    is_recurring = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=0),
                name="billing_expense_amount_positive",
            ),
        ]
        indexes = [
            models.Index(fields=["club", "date"]),
            models.Index(fields=["club", "is_recurring"]),
        ]

    def __str__(self):
        return f"{self.name}: {self.amount}"
