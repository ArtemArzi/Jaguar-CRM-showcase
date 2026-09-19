from django.contrib import admin

from apps.billing.models import Debt, Subscription, SubscriptionCorrection, Tariff, TrainingType

admin.site.register(TrainingType)


@admin.register(Tariff)
class TariffAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "club",
        "training_type",
        "scope",
        "location",
        "personal_booking_trainer",
        "is_personal_booking_default",
        "is_active",
    )
    list_filter = ("scope", "is_active", "is_personal_booking_default")
    raw_id_fields = ("personal_booking_trainer",)


admin.site.register(Debt)


class ReadOnlyBillingHistoryAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SubscriptionCorrection)
class SubscriptionCorrectionAdmin(ReadOnlyBillingHistoryAdmin):
    list_display = ("id", "club", "subscription", "component", "balance_delta", "actor", "channel", "created_at")
    list_filter = ("club", "channel")


@admin.register(Subscription)
class SubscriptionAdmin(ReadOnlyBillingHistoryAdmin):
    list_display = ("id", "club", "student", "status", "trainings_left", "trainings_used", "expires_at")
    list_filter = ("club", "status")
