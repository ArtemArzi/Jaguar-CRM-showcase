import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


@pytest.fixture
def migration_executor_with_restore():
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    yield executor
    MigrationExecutor(connection).migrate(latest)


@pytest.mark.django_db(transaction=True)
def test_unified_client_journey_flag_defaults_off_for_existing_clubs(
    migration_executor_with_restore,
):
    executor = migration_executor_with_restore
    executor.migrate([("clubs", "0007_min_trainings_to_freeze")])
    old_apps = executor.loader.project_state(
        [("clubs", "0007_min_trainings_to_freeze")]
    ).apps
    Club = old_apps.get_model("clubs", "Club")
    ClubSettings = old_apps.get_model("clubs", "ClubSettings")
    club = Club.objects.create(
        name="Migration test club",
        city="Test city",
        disciplines=[],
    )
    ClubSettings.objects.create(club_id=club.id)

    executor = MigrationExecutor(connection)
    executor.migrate([("clubs", "0008_clubsettings_unified_client_journey_enabled")])
    new_apps = executor.loader.project_state(
        [("clubs", "0008_clubsettings_unified_client_journey_enabled")]
    ).apps
    migrated_club_settings = new_apps.get_model("clubs", "ClubSettings")

    assert (
        migrated_club_settings.objects.get(
            club_id=club.id
        ).unified_client_journey_enabled
        is False
    )


@pytest.mark.django_db(transaction=True)
def test_commercial_journey_protocol_backfills_v1_and_reverses_without_enabling_v2(
    migration_executor_with_restore,
):
    executor = migration_executor_with_restore
    executor.migrate([("clubs", "0008_clubsettings_unified_client_journey_enabled")])
    old_apps = executor.loader.project_state(
        [("clubs", "0008_clubsettings_unified_client_journey_enabled")]
    ).apps
    Club = old_apps.get_model("clubs", "Club")
    ClubSettings = old_apps.get_model("clubs", "ClubSettings")
    enabled_club = Club.objects.create(name="Enabled club", city="Test city", disciplines=[])
    disabled_club = Club.objects.create(name="Disabled club", city="Test city", disciplines=[])
    ClubSettings.objects.create(club_id=enabled_club.id, unified_client_journey_enabled=True)
    ClubSettings.objects.create(club_id=disabled_club.id, unified_client_journey_enabled=False)

    executor = MigrationExecutor(connection)
    executor.migrate([("clubs", "0009_commercial_journey_protocol_version")])
    v2_apps = executor.loader.project_state(
        [("clubs", "0009_commercial_journey_protocol_version")]
    ).apps
    versioned_settings = v2_apps.get_model("clubs", "ClubSettings")

    assert list(
        versioned_settings.objects.order_by("club_id").values_list(
            "commercial_journey_protocol_version", flat=True
        )
    ) == ["v1", "v1"]

    executor = MigrationExecutor(connection)
    executor.migrate([("clubs", "0008_clubsettings_unified_client_journey_enabled")])
    rolled_back_apps = executor.loader.project_state(
        [("clubs", "0008_clubsettings_unified_client_journey_enabled")]
    ).apps
    rolled_back_settings = rolled_back_apps.get_model("clubs", "ClubSettings")
    assert list(
        rolled_back_settings.objects.order_by("club_id").values_list(
            "unified_client_journey_enabled", flat=True
        )
    ) == [True, False]
