from __future__ import annotations

import logging

from django.db import transaction

from apps.clubs.models import Club, ClubMembership, Location
from apps.common.exceptions import BusinessLogicError

logger = logging.getLogger(__name__)


def register_club(
    *,
    user,
    name: str,
    city: str,
    disciplines: list[str] | None = None,
    location_name: str = "",
) -> Club:
    """Create a new club with default location and owner membership."""
    if ClubMembership.objects.filter(user=user, role=ClubMembership.Role.OWNER).exists():
        raise BusinessLogicError("User already owns a club", code="already_owner")

    with transaction.atomic():
        club = Club.objects.create(
            name=name,
            city=city,
            disciplines=disciplines or [],
        )
        Location.objects.create(
            club=club,
            name=location_name or f"{name} - Main",
        )
        ClubMembership.objects.create(
            user=user,
            club=club,
            role=ClubMembership.Role.OWNER,
        )
        from apps.attendance.models import TrainingGroupRolloutState

        TrainingGroupRolloutState.objects.create(
            club=club,
            mode=TrainingGroupRolloutState.Mode.OFF,
        )

    logger.info("club_registered", extra={"club_id": club.id, "user_id": user.id})
    return club


def create_location(*, club_id: int, name: str, address: str = "") -> Location:
    location = Location.objects.create(club_id=club_id, name=name, address=address)
    logger.info("location_created", extra={"location_id": location.id, "club_id": club_id})
    return location


def update_club_settings(
    *,
    club_id: int,
    name: str | None = None,
    city: str | None = None,
    disciplines: list[str] | None = None,
    update_name: bool = False,
    update_city: bool = False,
    update_disciplines: bool = False,
) -> Club:
    club = Club.objects.get(id=club_id)
    update_fields = []
    if update_name:
        club.name = name
        update_fields.append("name")
    if update_city:
        club.city = city
        update_fields.append("city")
    if update_disciplines:
        club.disciplines = disciplines
        update_fields.append("disciplines")
    if not update_fields:
        return club
    club.full_clean()
    club.save(update_fields=[*update_fields, "updated_at"])
    logger.info("club_settings_updated", extra={"club_id": club_id, "fields": update_fields})
    return club


def update_location(*, location_id: int, club_id: int, name: str, address: str = "") -> Location:
    location = Location.objects.filter(club_id=club_id, id=location_id).first()
    if not location:
        raise BusinessLogicError("Локация не найдена", code="location_not_found")
    location.name = name
    location.address = address
    location.save(update_fields=["name", "address", "updated_at"])
    logger.info("location_updated", extra={"location_id": location.id, "club_id": club_id})
    return location


def delete_location(*, location_id: int, club_id: int) -> None:
    location = Location.objects.filter(club_id=club_id, id=location_id).first()
    if not location:
        raise BusinessLogicError("Локация не найдена", code="location_not_found")

    # Check FK references
    from apps.attendance.models import Schedule
    from apps.trainers.models import TrainerLocation

    schedule_count = Schedule.objects.filter(club_id=club_id, location=location).count()
    trainer_count = TrainerLocation.objects.for_club(club_id).filter(location=location).count()
    if schedule_count > 0 or trainer_count > 0:
        raise BusinessLogicError(
            f"Нельзя удалить: локация используется ({schedule_count} занятий, {trainer_count} тренеров)",
            code="location_in_use",
        )
    location.delete()
    logger.info("location_deleted", extra={"location_id": location_id, "club_id": club_id})
