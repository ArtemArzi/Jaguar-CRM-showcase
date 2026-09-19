from __future__ import annotations

import json
import logging
from unittest.mock import patch

import pytest
from django.test import override_settings

from apps.leads.models import LeadIntakeEvent
from apps.leads.tasks import send_lead_intake_telegram_task
from apps.students.models import Student
from apps.students.tests.factories import StudentFactory


class _FakeTelegramResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return b'{"ok": true, "result": {"message_id": 123}}'


def _event(club, **overrides):
    student = overrides.pop(
        "student",
        StudentFactory(
            club=club,
            first_name="Lead",
            last_name="",
            phone="+79174002121",
            status=Student.Status.LEAD,
            lead_status=Student.LeadStatus.NEW,
            source=Student.Source.WEBSITE,
        ),
    )
    data = {
        "club": club,
        "student": student,
        "goal": "Хочу попробовать",
        "preferred_format": LeadIntakeEvent.PreferredFormat.GROUP,
        "source_page": "/",
        "utm_source": "yandex",
        "privacy_policy_version": "2026-06-19",
        "consent_text_hash": "sha256:test-consent",
        "request_id": "req-task",
        "client_ip_hash": "iphash",
        "user_agent_hash": "uahash",
    }
    data.update(overrides)
    return LeadIntakeEvent.objects.create(**data)


@pytest.mark.django_db
class TestLeadIntakeTelegramTask:
    def test_sends_telegram_and_marks_event_sent(self, club):
        event = _event(club)

        with (
            override_settings(
                TELEGRAM_BOT_TOKEN="test-token",
                TELEGRAM_LEAD_CHAT_ID="123456",
                TELEGRAM_LEAD_MESSAGE_THREAD_ID=9,
                TELEGRAM_LEAD_MESSAGE_MODE="full",
                CRM_PUBLIC_BASE_URL="https://app.jaguar-fight-club.ru",
            ),
            patch("urllib.request.urlopen", return_value=_FakeTelegramResponse()) as urlopen,
        ):
            send_lead_intake_telegram_task(event.id, club.id)

        event.refresh_from_db()
        assert event.telegram_status == LeadIntakeEvent.TelegramStatus.SENT
        assert event.telegram_attempt_count == 1
        assert event.telegram_sent_at is not None
        assert event.telegram_error_code == ""

        request = urlopen.call_args.args[0]
        body = json.loads(request.data.decode())
        assert body["chat_id"] == "123456"
        assert body["message_thread_id"] == 9
        assert "Новая заявка" in body["text"]
        assert "Lead" in body["text"]
        assert "+79174002121" in body["text"]
        assert "sha256:test-consent" not in body["text"]
        assert "iphash" not in body["text"]
        assert f"https://app.jaguar-fight-club.ru/dashboard/students/{event.student_id}/card/" in body["text"]
        assert "/dashboard/leads/" not in body["text"]

    def test_repeat_lost_lead_message_includes_current_crm_status(self, club):
        student = StudentFactory(
            club=club,
            first_name="Lost",
            phone="+79174009902",
            status=Student.Status.LOST,
            lead_status=None,
            loss_reason=Student.LossReason.EXPENSIVE,
            source=Student.Source.WEBSITE,
        )
        event = _event(club, student=student, is_repeat_submission=True)

        with (
            override_settings(
                TELEGRAM_BOT_TOKEN="test-token",
                TELEGRAM_LEAD_CHAT_ID="123456",
                TELEGRAM_LEAD_MESSAGE_THREAD_ID=0,
                TELEGRAM_LEAD_MESSAGE_MODE="full",
                CRM_PUBLIC_BASE_URL="https://app.jaguar-fight-club.ru",
            ),
            patch("urllib.request.urlopen", return_value=_FakeTelegramResponse()) as urlopen,
        ):
            send_lead_intake_telegram_task(event.id, club.id)

        request = urlopen.call_args.args[0]
        body = json.loads(request.data.decode())
        assert "Повторная заявка: да" in body["text"]
        assert "Текущий статус CRM: Потерян · причина: Дорого" in body["text"]
        assert f"https://app.jaguar-fight-club.ru/dashboard/students/{event.student_id}/card/" in body["text"]

    def test_owner_review_message_does_not_link_a_soft_deleted_card(self, club):
        student = StudentFactory(club=club, status=Student.Status.LOST, lead_status=None)
        student.soft_delete()
        event = _event(
            club,
            student=student,
            is_repeat_submission=True,
            requires_owner_review=True,
        )

        with (
            override_settings(
                TELEGRAM_BOT_TOKEN="test-token",
                TELEGRAM_LEAD_CHAT_ID="123456",
                TELEGRAM_LEAD_MESSAGE_MODE="full",
                CRM_PUBLIC_BASE_URL="https://app.jaguar-fight-club.ru",
            ),
            patch("urllib.request.urlopen", return_value=_FakeTelegramResponse()) as urlopen,
        ):
            send_lead_intake_telegram_task(event.id, club.id)

        body = json.loads(urlopen.call_args.args[0].data.decode())
        assert "Требуется проверка руководителем: удалённая карточка" in body["text"]
        assert "/dashboard/students/" not in body["text"]

    def test_missing_telegram_settings_marks_event_skipped(self, club):
        event = _event(club)

        with override_settings(TELEGRAM_BOT_TOKEN="", TELEGRAM_LEAD_CHAT_ID=""):
            send_lead_intake_telegram_task(event.id, club.id)

        event.refresh_from_db()
        assert event.telegram_status == LeadIntakeEvent.TelegramStatus.SKIPPED
        assert event.telegram_attempt_count == 0
        assert event.telegram_error_code == "telegram_not_configured"

    def test_delivery_failure_marks_failed_without_raw_error_details(self, club, caplog):
        event = _event(club)

        with (
            caplog.at_level(logging.WARNING, logger="apps.leads.tasks"),
            override_settings(
                TELEGRAM_BOT_TOKEN="test-token",
                TELEGRAM_LEAD_CHAT_ID="123456",
                TELEGRAM_LEAD_MESSAGE_THREAD_ID=0,
                TELEGRAM_LEAD_MESSAGE_MODE="full",
            ),
            patch("urllib.request.urlopen", side_effect=OSError("timeout for +79174002121")),
        ):
            send_lead_intake_telegram_task(event.id, club.id)

        event.refresh_from_db()
        assert event.telegram_status == LeadIntakeEvent.TelegramStatus.FAILED
        assert event.telegram_attempt_count == 1
        assert event.telegram_error_code == "telegram_delivery_failed"
        assert "+79174002121" not in event.telegram_error_code
        assert "+79174002121" not in caplog.text
        assert "test-token" not in caplog.text
        assert "123456" not in caplog.text
