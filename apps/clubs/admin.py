from django.contrib import admin

from apps.clubs.models import Club, ClubMembership, Location


@admin.register(Club)
class ClubAdmin(admin.ModelAdmin):
    list_display = ["name", "city", "is_active", "created_at"]
    list_filter = ["is_active", "city"]


@admin.register(Location)
class LocationAdmin(admin.ModelAdmin):
    list_display = ["name", "club", "address"]
    list_filter = ["club"]


@admin.register(ClubMembership)
class ClubMembershipAdmin(admin.ModelAdmin):
    list_display = ["user", "club", "role", "is_active"]
    list_filter = ["role", "is_active"]
