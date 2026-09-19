import pytest
from django.core.management import CommandError, call_command

from apps.billing.models import Payment, Subscription, TrainingType
from apps.clubs.models import Club, ClubMembership
from apps.clubs.tests.factories import ClubFactory
from apps.students.models import Student
from apps.trainers.models import Trainer, TrainerEarning


@pytest.mark.django_db
def test_seed_test_data_new_club_repeated_name_creates_unique_demo_users():
    for _ in range(2):
        call_command(
            "seed_test_data",
            new_club=True,
            name="Jaguar Muay Thai",
            city="Moscow",
            students=2,
            schedules=1,
            checkins=0,
            locations=1,
            trainers=1,
            training_types=1,
            grade_systems=0,
            verbosity=0,
        )

    clubs = list(Club.objects.filter(name="Jaguar Muay Thai").order_by("id"))

    assert len(clubs) == 2

    expected_roles = {"owner", "admin", "trainer", "student", "parent"}
    for club in clubs:
        role_usernames = {
            membership.role: membership.user.username
            for membership in ClubMembership.objects.filter(club=club).select_related("user")
        }

        assert set(role_usernames) == expected_roles
        for role in expected_roles:
            assert role_usernames[role] == f"club{club.id}_{role}"

        assert Trainer.objects.for_club(club).filter(
            user__username=f"club{club.id}_trainer",
        ).exists()
        assert Student.objects.for_club(club).filter(
            user__username=f"club{club.id}_student",
            status__in=[Student.Status.ACTIVE, Student.Status.TRIAL],
        ).exists()
        assert Student.objects.for_club(club).filter(
            parent_user__username=f"club{club.id}_parent",
            is_child=True,
        ).exists()
        assert Subscription.objects.for_club(club).count() >= 2
        assert Payment.objects.for_club(club).filter(
            recorded_by__username=f"club{club.id}_admin",
        ).exists()
        assert Payment.objects.for_club(club).filter(seller_trainer__isnull=False).exists()
        assert list(
            TrainingType.objects.for_club(club).values_list("kind", flat=True)
        ) == [TrainingType.Kind.GROUP]


@pytest.mark.django_db
def test_seed_test_data_existing_club_rejects_second_seed_run():
    club = ClubFactory(name="Existing Demo Club", city="Moscow")

    call_command(
        "seed_test_data",
        club_id=club.id,
        students=2,
        schedules=1,
        checkins=0,
        locations=1,
        trainers=1,
        training_types=1,
        grade_systems=0,
        verbosity=0,
    )

    with pytest.raises(CommandError, match="already appears to contain seed_test_data"):
        call_command(
            "seed_test_data",
            club_id=club.id,
            students=2,
            schedules=1,
            checkins=0,
            locations=1,
            trainers=1,
            training_types=1,
            grade_systems=0,
            verbosity=0,
        )


@pytest.mark.django_db
def test_seed_test_data_rejects_invalid_required_counts():
    with pytest.raises(CommandError, match="--students must be at least 1"):
        call_command(
            "seed_test_data",
            new_club=True,
            students=0,
            schedules=1,
            checkins=0,
            locations=1,
            trainers=1,
            training_types=1,
            grade_systems=0,
            verbosity=0,
        )


@pytest.mark.django_db
def test_seed_test_data_checkin_earnings_follow_training_type_kind():
    call_command(
        "seed_test_data",
        new_club=True,
        name="Jaguar Salary Demo",
        city="Moscow",
        students=3,
        schedules=3,
        checkins=3,
        locations=1,
        trainers=1,
        training_types=3,
        grade_systems=0,
        verbosity=0,
    )

    club = Club.objects.get(name="Jaguar Salary Demo")
    kinds = list(
        TrainingType.objects.for_club(club).order_by("slug").values_list("kind", flat=True)
    )
    earning_types = set(
        TrainerEarning.objects.for_club(club).values_list("earning_type", flat=True)
    )

    assert kinds == [
        TrainingType.Kind.GROUP,
        TrainingType.Kind.PERSONAL,
        TrainingType.Kind.MINI_GROUP,
    ]
    assert earning_types == {TrainingType.Kind.PERSONAL, TrainingType.Kind.MINI_GROUP}
