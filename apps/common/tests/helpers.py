from apps.clubs.models import ClubMembership
from apps.clubs.tests.factories import ClubMembershipFactory


def make_auth_params(user, club, role="owner"):
    """Build request kwargs to simulate authenticated API calls in tests."""
    membership = ClubMembership.objects.filter(user=user, club=club).first()
    if not membership:
        membership = ClubMembershipFactory(user=user, club=club, role=role)
    return {"user": user, "club": club, "_membership": membership, "auth": {"user_id": user.id}}
