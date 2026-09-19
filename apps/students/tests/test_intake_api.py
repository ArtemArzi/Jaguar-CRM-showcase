from uuid import uuid4

import pytest
from ninja.testing import TestClient

from apps.clubs.models import ClubSettings
from apps.clubs.tests.factories import ClubMembershipFactory, ClubSettingsFactory, UserFactory
from apps.common.tests.helpers import make_auth_params as _auth_params
from apps.students.models import Student
from config.api import api

client = TestClient(api)


@pytest.fixture(autouse=True)
def _bypass_jwt_auth(bypass_jwt_auth):
    pass


def _payload(**overrides):
    payload = {
        "idempotency_key": str(uuid4()),
        "intake_kind": "existing_student",
        "first_name": "Ivan",
        "last_name": "Petrov",
        "phone": "+79001234567",
        "guardian_phone": "",
        "is_child": False,
        "source": "other",
        "confirm_distinct_child": False,
    }
    payload.update(overrides)
    return payload


@pytest.mark.django_db
def test_intake_capability_is_dual_gated_and_tenant_scoped(club, other_club, owner_user, settings):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = False
    ClubSettings.objects.get_or_create(
        club=club,
        defaults={"unified_client_journey_enabled": True},
    )
    assert client.get("/students/intakes/capability", **_auth_params(owner_user, club)).json() == {
        "enabled": False,
        "group_sale_command_protocol_version": "v1",
    }

    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    ClubSettings.objects.filter(club=club).update(unified_client_journey_enabled=False)
    ClubSettingsFactory(club=other_club, unified_client_journey_enabled=True)
    assert client.get("/students/intakes/capability", **_auth_params(owner_user, club)).json() == {
        "enabled": False,
        "group_sale_command_protocol_version": "v1",
    }

    ClubSettings.objects.filter(club=club).update(unified_client_journey_enabled=True)
    assert client.get("/students/intakes/capability", **_auth_params(owner_user, club)).json() == {
        "enabled": True,
        "group_sale_command_protocol_version": "v1",
    }


@pytest.mark.django_db
def test_intake_post_fails_closed_when_dual_capability_is_off(club, owner_user, settings):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = False
    ClubSettingsFactory(club=club, unified_client_journey_enabled=True)

    response = client.post(
        "/students/intakes/",
        json=_payload(),
        **_auth_params(owner_user, club),
    )

    assert response.status_code == 400
    assert response.json()["code"] == "unified_client_journey_disabled"
    assert not Student.objects.for_club(club).exists()


@pytest.mark.django_db
def test_intake_post_returns_safe_existing_student_receipt_when_enabled(club, owner_user, settings):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    ClubSettingsFactory(club=club, unified_client_journey_enabled=True)

    response = client.post(
        "/students/intakes/",
        json=_payload(),
        **_auth_params(owner_user, club),
    )

    assert response.status_code == 201
    receipt = response.json()
    assert receipt["result_kind"] == "created_existing_student"
    assert receipt["target_workspace"] == "students"
    assert receipt["identity_visibility"] == "full"
    assert receipt["allowed_action"] == "open"
    assert receipt["route"].startswith("/dashboard/students/")
    assert receipt["code"] == "no_crm_entitlement"
    assert receipt["commercial_segment"] == "no_crm_entitlement"
    assert "+79001234567" not in str(receipt)


@pytest.mark.django_db
def test_intake_post_allows_same_named_child_siblings_with_distinct_birth_dates(
    club,
    owner_user,
    settings,
):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    ClubSettingsFactory(club=club, unified_client_journey_enabled=True)
    shared_identity = {
        "intake_kind": "new_contact",
        "first_name": "Masha",
        "last_name": "Petrova",
        "phone": "",
        "guardian_phone": "+79001234567",
        "is_child": True,
    }

    first = client.post(
        "/students/intakes/",
        json=_payload(**shared_identity, date_of_birth="2017-05-06"),
        **_auth_params(owner_user, club),
    )
    sibling = client.post(
        "/students/intakes/",
        json=_payload(**shared_identity, date_of_birth="2018-05-06"),
        **_auth_params(owner_user, club),
    )

    assert first.status_code == sibling.status_code == 201
    assert first.json()["result_kind"] == sibling.json()["result_kind"] == "created_new_contact"
    assert first.json()["student_id"] != sibling.json()["student_id"]
    assert Student.objects.for_club(club).filter(guardian_phone="+79001234567").count() == 2


@pytest.mark.django_db
@pytest.mark.parametrize("intake_kind", ["new_contact", "existing_student"])
@pytest.mark.parametrize(
    ("field", "value", "status_code", "code"),
    [
        ("first_name", "   ", 400, "first_name_required"),
        ("first_name", "A" * 101, 422, None),
        ("last_name", "B" * 101, 422, None),
    ],
)
def test_intake_post_rejects_invalid_names_without_creating_a_student(
    club,
    owner_user,
    settings,
    intake_kind,
    field,
    value,
    status_code,
    code,
):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    ClubSettingsFactory(club=club, unified_client_journey_enabled=True)

    response = client.post(
        "/students/intakes/",
        json=_payload(intake_kind=intake_kind, **{field: value}),
        **_auth_params(owner_user, club),
    )

    assert response.status_code == status_code
    if code is not None:
        assert response.json() == {"detail": "First name is required", "code": code}
    else:
        assert response.json()["detail"][0]["loc"] == ["body", "payload", field]
    assert not Student.objects.for_club(club).exists()


@pytest.mark.django_db
def test_intake_post_authorizes_admin_and_denies_student_and_other_tenant(
    club, other_club, admin_user, student_user, settings
):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    ClubSettings.objects.get_or_create(club=club, defaults={"unified_client_journey_enabled": True})
    ClubSettings.objects.filter(club=club).update(unified_client_journey_enabled=True)

    admin_response = client.post(
        "/students/intakes/",
        json=_payload(),
        **_auth_params(admin_user, club, role="admin"),
    )
    assert admin_response.status_code == 201

    denied_response = client.post(
        "/students/intakes/",
        json=_payload(phone="+79001234568"),
        **_auth_params(student_user, club, role="student"),
    )
    assert denied_response.status_code == 403

    foreign_user = UserFactory()
    ClubMembershipFactory(user=foreign_user, club=other_club, role="owner")
    foreign_response = client.post(
        "/students/intakes/",
        json=_payload(phone="+79001234569"),
        **_auth_params(foreign_user, other_club),
    )
    assert foreign_response.status_code == 400
    assert foreign_response.json()["code"] == "unified_client_journey_disabled"
    assert not Student.objects.for_club(other_club).exists()
