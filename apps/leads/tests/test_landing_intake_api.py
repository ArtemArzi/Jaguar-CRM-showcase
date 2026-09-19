from __future__ import annotations

from unittest.mock import patch

import pytest
from django.test import Client, override_settings
from ninja.testing import TestClient

from apps.leads.models import LeadIntakeEvent
from apps.students.models import Student
from config.api import api

client = TestClient(api)
django_client = Client()


def _payload(**overrides) -> dict:
    data = {
        "name": "Landing Visitor",
        "phone": "8 (917) 400-21-21",
        "goal": "Хочу попробовать",
        "preferred_format": "group",
        "is_child": False,
        "consent": {
            "personal_data": True,
            "privacy_policy_version": "2026-06-19",
            "consent_text_hash": "sha256:test-consent",
        },
        "source": {
            "page": "/",
            "utm_source": "yandex",
            "utm_medium": "cpc",
            "utm_campaign": "trial",
            "utm_content": "hero",
            "utm_term": "muay-thai",
        },
        "idempotency_key": "44444444-4444-4444-4444-444444444444",
        "hp_field": "",
    }
    data.update(overrides)
    return data


@pytest.mark.django_db
class TestPublicLeadIntakeAPI:
    def test_public_endpoint_cors_allows_landing_request_id_header(self):
        with override_settings(CORS_ALLOWED_ORIGINS=["https://jaguar-fight-club.ru"]):
            response = django_client.options(
                "/api/public/lead-intakes/",
                HTTP_ORIGIN="https://jaguar-fight-club.ru",
                HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
                HTTP_ACCESS_CONTROL_REQUEST_HEADERS="content-type,x-request-id",
            )

        assert response.status_code == 200
        assert response["access-control-allow-origin"] == "https://jaguar-fight-club.ru"
        assert "x-request-id" in response["access-control-allow-headers"].lower()

    def test_public_endpoint_accepts_landing_lead_without_auth(self, club):
        with override_settings(LANDING_DEFAULT_CLUB_ID=club.id), patch("django_q.tasks.async_task"):
            response = client.post("/public/lead-intakes/", json=_payload())

        assert response.status_code == 201
        data = response.json()["data"]
        assert data["status"] == "accepted"

        event = LeadIntakeEvent.objects.get(id=data["id"])
        assert event.club_id == club.id
        assert event.student.phone == "+79174002121"
        assert event.student.source == Student.Source.WEBSITE

    def test_client_cannot_choose_club_id(self, club, other_club):
        with override_settings(LANDING_DEFAULT_CLUB_ID=club.id), patch("django_q.tasks.async_task"):
            response = client.post(
                "/public/lead-intakes/",
                json=_payload(club_id=other_club.id, assigned_trainer_id=999, lead_status="contacted"),
            )

        assert response.status_code == 201
        event = LeadIntakeEvent.objects.get(id=response.json()["data"]["id"])
        assert event.club_id == club.id
        assert event.student.club_id == club.id
        assert not Student.objects.filter(club=other_club, phone="+79174002121").exists()

    def test_soft_deleted_exact_public_identity_is_accepted_without_disclosing_student_identity(self, club):
        existing = Student.objects.create(
            club=club,
            first_name="Deleted",
            last_name="",
            phone="+79174002121",
            status=Student.Status.LOST,
            lead_status=None,
        )
        existing.soft_delete()

        with override_settings(LANDING_DEFAULT_CLUB_ID=club.id), patch("django_q.tasks.async_task"):
            response = client.post("/public/lead-intakes/", json=_payload())

        assert response.status_code == 201
        assert set(response.json()["data"]) == {"id", "status"}
        assert "student" not in response.json()
        event = LeadIntakeEvent.objects.get(id=response.json()["data"]["id"])
        assert event.student_id == existing.id
        assert event.requires_owner_review is True
        assert Student.objects.for_club(club).filter(phone="+79174002121").count() == 1

    def test_missing_consent_rejects_without_creating_lead(self, club):
        payload = _payload()
        payload["consent"]["personal_data"] = False

        with override_settings(LANDING_DEFAULT_CLUB_ID=club.id), patch("django_q.tasks.async_task"):
            response = client.post("/public/lead-intakes/", json=payload)

        assert response.status_code == 400
        assert response.json()["code"] == "consent_required"
        assert LeadIntakeEvent.objects.count() == 0
        assert Student.objects.filter(phone="+79174002121").count() == 0

    def test_honeypot_rejects_without_creating_lead(self, club):
        with override_settings(LANDING_DEFAULT_CLUB_ID=club.id), patch("django_q.tasks.async_task"):
            response = client.post("/public/lead-intakes/", json=_payload(hp_field="bot-value"))

        assert response.status_code == 400
        assert response.json()["code"] == "spam_detected"
        assert LeadIntakeEvent.objects.count() == 0
