from django.db import models


class TenantQuerySet(models.QuerySet):
    def for_club(self, club):
        return self.filter(club=club)


class TenantManager(models.Manager):
    def get_queryset(self):
        return TenantQuerySet(self.model, using=self._db)

    def for_club(self, club):
        return self.get_queryset().for_club(club)

    def unscoped(self):
        """Only for superadmin / management commands."""
        return super().get_queryset()
