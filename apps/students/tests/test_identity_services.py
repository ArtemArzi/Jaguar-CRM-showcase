import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Barrier
from urllib.parse import urlparse
from uuid import UUID

import pytest
from django.db import close_old_connections, connection

from apps.common.exceptions import BusinessLogicError
from apps.leads.services import create_landing_lead_intake, create_lead
from apps.students.identity_services import (
    arbitrate_person_identity,
    normalize_person_identity,
)
from apps.students.models import Student
from apps.students.services import create_student
from apps.students.tests.factories import StudentFactory


def _has_disposable_purpose_token(database_name: str) -> bool:
    return bool(re.search(r"(?:^|[_-])(?:test|e2e|journey)(?:[_-]|$)", database_name.lower()))


@pytest.mark.django_db
def test_postgresql_race_gate_requires_an_opt_in_disposable_local_database():
    database_url = os.environ.get("UNIFIED_CLIENT_JOURNEY_POSTGRES_URL", "")
    gate_required = os.environ.get("UNIFIED_CLIENT_JOURNEY_POSTGRES_GATE_REQUIRED") == "1"
    if not database_url and not gate_required:
        pytest.skip("unified client journey PostgreSQL race gate is opt-in")
    assert database_url, "UNIFIED_CLIENT_JOURNEY_POSTGRES_URL is required"

    parsed = urlparse(database_url)
    assert parsed.scheme in {"postgres", "postgresql"}
    assert parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    assert not parsed.query, "PostgreSQL gate URL must not contain query parameters"
    database_name = parsed.path.lstrip("/")
    assert database_name
    assert _has_disposable_purpose_token(database_name)

    assert connection.vendor == "postgresql"
    active_database_name = str(connection.settings_dict["NAME"])
    assert active_database_name in {database_name, f"test_{database_name}"}
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_database()")
        assert cursor.fetchone() == (active_database_name,)


@pytest.mark.django_db
def test_normalization_and_arbitration_preserve_child_siblings_and_tenant_isolation(club, other_club):
    identity = normalize_person_identity(
        first_name="Masha",
        last_name="Petrova",
        is_child=True,
        phone="8 (917) 400-20-20",
    )
    StudentFactory(
        club=other_club,
        first_name="Masha",
        last_name="Petrova",
        is_child=True,
        phone="",
        guardian_phone=identity.guardian_phone,
    )

    from django.db import transaction

    with transaction.atomic():
        assert arbitrate_person_identity(
            club_id=club.id,
            identity=identity,
            purpose="staff_create",
        ) is None


@pytest.mark.skipif(connection.vendor != "postgresql", reason="requires PostgreSQL row locking")
@pytest.mark.django_db(transaction=True)
def test_postgresql_same_named_siblings_with_distinct_birth_dates_are_not_collapsed(club):
    barrier = Barrier(2)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda date_of_birth: _race_worker(
                    barrier=barrier,
                    create=lambda: create_lead(
                        club_id=club.id,
                        first_name="Masha",
                        last_name="Petrova",
                        phone="",
                        guardian_phone="+79174002020",
                        is_child=True,
                        date_of_birth=date_of_birth,
                    ),
                ),
                (date(2017, 5, 6), date(2018, 5, 6)),
            )
        )

    assert [result[0] for result in results] == ["created", "created"]
    assert Student.objects.for_club(club).filter(guardian_phone="+79174002020").count() == 2


@pytest.mark.django_db
def test_public_identity_arbitration_reuses_existing_adult_without_changing_it(club):
    existing = StudentFactory(
        club=club,
        first_name="Existing",
        phone="+79174002021",
        status=Student.Status.ACTIVE,
        lead_status=None,
    )
    identity = normalize_person_identity(
        first_name="New Name",
        is_child=False,
        phone="8 (917) 400-20-21",
    )

    from django.db import transaction

    with transaction.atomic():
        resolved = arbitrate_person_identity(
            club_id=club.id,
            identity=identity,
            purpose="public_intake",
        )

    assert resolved is not None
    assert resolved.id == existing.id


def _race_worker(*, barrier: Barrier, create):
    close_old_connections()
    try:
        barrier.wait(timeout=10)
        student = create()
        return "created", student.id
    except BusinessLogicError as error:
        return "duplicate", error.code
    except Exception as error:  # pragma: no cover - assertion below preserves its type
        return "unexpected", type(error).__name__
    finally:
        close_old_connections()


@pytest.mark.skipif(connection.vendor != "postgresql", reason="requires PostgreSQL row locking")
@pytest.mark.django_db(transaction=True)
def test_postgresql_staff_create_race_serializes_on_one_club_row(club):
    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda create: _race_worker(barrier=barrier, create=create),
                (
                    lambda: create_student(
                        club_id=club.id,
                        first_name="Race",
                        last_name="Student",
                        phone="8 (917) 400-20-30",
                    ),
                    lambda: create_lead(
                        club_id=club.id,
                        first_name="Race",
                        last_name="Lead",
                        phone="+7 917 400 20 30",
                    ),
                ),
            )
        )

    assert sorted(result[0] for result in results) == ["created", "duplicate"]
    assert all(result[1] != "IntegrityError" for result in results)
    assert Student.objects.for_club(club).filter(phone="+79174002030").count() == 1


@pytest.mark.skipif(connection.vendor != "postgresql", reason="requires PostgreSQL row locking")
@pytest.mark.django_db(transaction=True)
def test_postgresql_public_and_staff_race_serializes_person_creation(club, monkeypatch):
    monkeypatch.setattr("apps.leads.services._queue_lead_intake_telegram", lambda **kwargs: None)
    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda create: _race_worker(barrier=barrier, create=create),
                (
                    lambda: create_landing_lead_intake(
                        club_id=club.id,
                        name="Public Race",
                        phone="8 (917) 400-20-31",
                        goal="Group training",
                        preferred_format="group",
                        is_child=False,
                        consent={
                            "personal_data": True,
                            "privacy_policy_version": "test",
                            "consent_text_hash": "hash",
                        },
                        source={},
                        request_id="race-public",
                        client_ip_hash="hash",
                        user_agent="test",
                        idempotency_key=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
                    ).student,
                    lambda: create_student(
                        club_id=club.id,
                        first_name="Staff Race",
                        last_name="",
                        phone="+7 917 400 20 31",
                    ),
                ),
            )
        )

    assert set(result[0] for result in results) in ({"created"}, {"created", "duplicate"})
    assert all(result[1] != "IntegrityError" for result in results)
    assert Student.objects.for_club(club).filter(phone="+79174002031").count() == 1
    from apps.leads.models import LeadIntakeEvent

    assert LeadIntakeEvent.objects.for_club(club).count() == 1


@pytest.mark.skipif(connection.vendor != "postgresql", reason="requires PostgreSQL row locking")
@pytest.mark.django_db(transaction=True)
def test_postgresql_same_public_idempotency_key_creates_one_event_and_person(club, monkeypatch):
    monkeypatch.setattr("apps.leads.services._queue_lead_intake_telegram", lambda **kwargs: None)
    barrier = Barrier(2)
    key = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

    def public_submission():
        return create_landing_lead_intake(
            club_id=club.id,
            name="Same Key Race",
            phone="8 (917) 400-20-32",
            goal="Group training",
            preferred_format="group",
            is_child=False,
            consent={
                "personal_data": True,
                "privacy_policy_version": "test",
                "consent_text_hash": "hash",
            },
            source={},
            request_id="same-key-race",
            client_ip_hash="hash",
            user_agent="test",
            idempotency_key=key,
        ).student

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda create: _race_worker(barrier=barrier, create=create),
                (public_submission, public_submission),
            )
        )

    assert [result[0] for result in results] == ["created", "created"]
    assert Student.objects.for_club(club).filter(phone="+79174002032").count() == 1
    from apps.leads.models import LeadIntakeEvent

    assert LeadIntakeEvent.objects.for_club(club).filter(idempotency_key=key).count() == 1
