from django.db import models

from apps.common.managers import TenantManager


class BaseModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class SoftDeleteMixin(models.Model):
    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        abstract = True

    def soft_delete(self):
        from django.utils import timezone

        self.deleted_at = timezone.now()
        self.save(update_fields=["deleted_at"])


class TenantMixin(BaseModel):
    club = models.ForeignKey(
        "clubs.Club",
        on_delete=models.PROTECT,
        related_name="%(class)ss",
        db_index=True,
    )
    objects = TenantManager()

    class Meta:
        abstract = True
