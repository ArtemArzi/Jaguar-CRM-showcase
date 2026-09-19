from __future__ import annotations

import inspect
from datetime import UTC, datetime
from unittest.mock import patch
from uuid import UUID

import pytest
from django.db import transaction

import apps.leads.service_modules.intake as intake_lifecycle
from apps.common.exceptions import BusinessLogicError
from apps.leads.models import LeadIntakeEvent
from apps.leads.selectors import get_leads
from apps.leads.services import create_landing_lead_intake
from apps.retention.models import RetentionTask
from apps.students.models import Student
from apps.students.tests.factories import StudentFactory
from apps.trainers.tests.factories import TrainerFactory


def _consent() -> dict:
    return {
        "personal_data": True,
        "privacy_policy_version": "2026-06-19",
        "consent_text_hash": "sha256:test-consent",
    }


def _source(**overrides) -> dict:
    data = {
        "page": "/",
        "utm_source": "yandex",
        "utm_medium": "cpc",
        "utm_campaign": "trial",
        "utm_content": "hero",
        "utm_term": "muay-thai",
    }
    data.update(overrides)
    return data


def _submit_public_intake_for_phone(club, *, phone: str, idempotency_key: UUID) -> LeadIntakeEvent:
    return create_landing_lead_intake(
        club_id=club.id,
        name="Repeat Public",
        phone=phone,
        goal="Хочу снова обсудить тренировки",
        preferred_format="group",
        is_child=False,
        consent=_consent(),
        source=_source(page="/repeat"),
        request_id=f"req-{idempotency_key}",
        client_ip_hash="iphash",
        user_agent="Mozilla/5.0",
        idempotency_key=idempotency_key,
    )


@pytest.mark.django_db(transaction=True)
class TestCreateLandingLeadIntake:
    def test_new_public_intake_locks_club_before_transactional_idempotency_recheck(self):
        source = inspect.getsource(intake_lifecycle.create_landing_lead_intake)

        transaction_start = source.index("with transaction.atomic():")
        lock_index = source.index("lock_club_person_identity_arbitration", transaction_start)
        idempotency_index = source.index("LeadIntakeEvent.objects.for_club", transaction_start)
        resolve_index = source.index("resolve_person_identity", transaction_start)

        assert lock_index < idempotency_index < resolve_index

    def test_creates_student_event_and_queues_telegram_after_commit(self, club):
        with patch("django_q.tasks.async_task") as async_task:
            with transaction.atomic():
                event = create_landing_lead_intake(
                    club_id=club.id,
                    name="  Artem Landing  ",
                    phone="8 (917) 400-21-21",
                    goal="Хочу форму и разгрузку после работы",
                    preferred_format="hybrid",
                    is_child=False,
                    consent=_consent(),
                    source=_source(),
                    request_id="req-public-1",
                    client_ip_hash="iphash",
                    user_agent="Mozilla/5.0",
                    idempotency_key=UUID("11111111-1111-1111-1111-111111111111"),
                )
                assert async_task.call_count == 0

            async_task.assert_called_once_with(
                "apps.leads.tasks.send_lead_intake_telegram_task",
                event.id,
                club.id,
            )

        student = Student.objects.get(id=event.student_id)
        assert student.club_id == club.id
        assert student.first_name == "Artem Landing"
        assert student.last_name == ""
        assert student.phone == "+79174002121"
        assert student.status == Student.Status.LEAD
        assert student.lead_status == Student.LeadStatus.NEW
        assert student.source == Student.Source.WEBSITE
        assert get_leads(club=club).filter(id=student.id).exists()
        assert not RetentionTask.objects.for_club(club).filter(
            student=student,
            task_type=RetentionTask.TaskType.NEW_LEAD,
        ).exists()

        event.refresh_from_db()
        assert event.goal == "Хочу форму и разгрузку после работы"
        assert event.preferred_format == LeadIntakeEvent.PreferredFormat.HYBRID
        assert event.source_page == "/"
        assert event.utm_source == "yandex"
        assert event.privacy_policy_version == "2026-06-19"
        assert event.consent_text_hash == "sha256:test-consent"
        assert event.request_id == "req-public-1"
        assert event.client_ip_hash == "iphash"
        assert event.user_agent_hash
        assert event.idempotency_key == UUID("11111111-1111-1111-1111-111111111111")
        assert event.telegram_status == LeadIntakeEvent.TelegramStatus.PENDING
        assert event.is_repeat_submission is False

    def test_facade_queue_patch_controls_extracted_intake(self, club):
        with patch("apps.leads.services._queue_lead_intake_telegram") as queue_telegram:
            event = create_landing_lead_intake(
                club_id=club.id,
                name="Facade Callback",
                phone="8 (917) 400-21-22",
                goal="Хочу попробовать",
                preferred_format="group",
                is_child=False,
                consent=_consent(),
                source=_source(),
                request_id="req-facade-callback",
                client_ip_hash="iphash",
                user_agent="Mozilla/5.0",
                idempotency_key=UUID("12121212-1212-1212-1212-121212121210"),
            )

        queue_telegram.assert_called_once_with(event_id=event.id, club_id=club.id)

    def test_facade_clock_patch_controls_extracted_intake(self, club):
        accepted_at = datetime(2026, 8, 31, 9, 45, 12, tzinfo=UTC)

        with (
            patch("django_q.tasks.async_task"),
            patch("apps.leads.services.timezone.now", return_value=accepted_at),
        ):
            event = create_landing_lead_intake(
                club_id=club.id,
                name="Facade Clock",
                phone="8 (917) 400-21-23",
                goal="Хочу попробовать",
                preferred_format="group",
                is_child=False,
                consent=_consent(),
                source=_source(),
                request_id="req-facade-clock",
                client_ip_hash="iphash",
                user_agent="Mozilla/5.0",
                idempotency_key=UUID("13131313-1313-1313-1313-131313131310"),
            )

        event.refresh_from_db()
        assert event.consent_accepted_at == accepted_at

    def test_outer_transaction_rollback_does_not_enqueue_telegram(self, club):
        with patch("django_q.tasks.async_task") as async_task:
            with pytest.raises(RuntimeError, match="rollback outer transaction"):
                with transaction.atomic():
                    create_landing_lead_intake(
                        club_id=club.id,
                        name="Rollback Landing",
                        phone="8 (917) 400-21-20",
                        goal="Хочу попробовать",
                        preferred_format="group",
                        is_child=False,
                        consent=_consent(),
                        source=_source(),
                        request_id="req-rollback",
                        client_ip_hash="iphash",
                        user_agent="Mozilla/5.0",
                        idempotency_key=UUID("10101010-1010-1010-1010-101010101010"),
                    )
                    async_task.assert_not_called()
                    raise RuntimeError("rollback outer transaction")

            async_task.assert_not_called()

        assert LeadIntakeEvent.objects.for_club(club).count() == 0

    def test_duplicate_phone_attaches_new_event_to_existing_student(self, club):
        existing = StudentFactory(
            club=club,
            first_name="Existing",
            phone="+79174002121",
            status=Student.Status.LEAD,
            lead_status=Student.LeadStatus.CONTACTED,
        )

        with patch("django_q.tasks.async_task"):
            event = create_landing_lead_intake(
                club_id=club.id,
                name="New Name",
                phone="+7 917 400 21 21",
                goal="Хочу тренировки",
                preferred_format="unsure",
                is_child=False,
                consent=_consent(),
                source=_source(page="/repeat"),
                request_id="req-repeat",
                client_ip_hash="iphash",
                user_agent="Mozilla/5.0",
                idempotency_key=UUID("22222222-2222-2222-2222-222222222222"),
            )

        assert event.student_id == existing.id
        assert event.is_repeat_submission is True
        assert Student.objects.filter(club=club, phone="+79174002121").count() == 1
        existing.refresh_from_db()
        assert existing.first_name == "Existing"
        assert existing.lead_status == Student.LeadStatus.CONTACTED

    def test_soft_deleted_public_identity_keeps_one_card_and_appends_safe_repeat_event(self, club):
        existing = StudentFactory(
            club=club,
            first_name="Deleted",
            phone="+79174002129",
            status=Student.Status.LOST,
            lead_status=None,
        )
        existing.soft_delete()

        with patch("django_q.tasks.async_task"):
            event = create_landing_lead_intake(
                club_id=club.id,
                name="Deleted retry",
                phone="+7 917 400 21 29",
                goal="Хочу тренировки",
                preferred_format="group",
                is_child=False,
                consent=_consent(),
                source=_source(page="/repeat"),
                request_id="req-soft-deleted",
                client_ip_hash="iphash",
                user_agent="Mozilla/5.0",
                idempotency_key=UUID("23232323-2323-2323-2323-232323232323"),
            )

        assert event.student_id == existing.id
        assert event.is_repeat_submission is True
        assert event.requires_owner_review is True
        assert Student.objects.for_club(club).filter(phone="+79174002129").count() == 1
        existing.refresh_from_db()
        assert existing.deleted_at is not None
        assert existing.status == Student.Status.LOST

    def test_child_submission_stores_submitted_phone_as_guardian_phone(self, club):
        with patch("django_q.tasks.async_task"):
            event = create_landing_lead_intake(
                club_id=club.id,
                name="Child Landing",
                phone="+7 917 400 22 22",
                goal="Ищу тренировки для ребенка",
                preferred_format="group",
                is_child=True,
                consent=_consent(),
                source=_source(page="/kids"),
                request_id="req-child",
                client_ip_hash="iphash",
                user_agent="Mozilla/5.0",
                idempotency_key=UUID("12121212-1212-1212-1212-121212121212"),
            )

        student = Student.objects.get(id=event.student_id)
        assert student.is_child is True
        assert student.phone == ""
        assert student.guardian_phone == "+79174002222"

    def test_child_submission_same_guardian_same_name_reuses_child_card(self, club):
        existing = StudentFactory(
            club=club,
            first_name="Same Child",
            last_name="",
            is_child=True,
            phone="",
            guardian_phone="+79174002225",
            status=Student.Status.LEAD,
            lead_status=Student.LeadStatus.NEW,
        )

        with patch("django_q.tasks.async_task"):
            event = create_landing_lead_intake(
                club_id=club.id,
                name="Same Child",
                phone="+7 917 400 22 25",
                goal="Повторная заявка для ребенка",
                preferred_format="group",
                is_child=True,
                consent=_consent(),
                source=_source(page="/kids"),
                request_id="req-child-same-name",
                client_ip_hash="iphash",
                user_agent="Mozilla/5.0",
                idempotency_key=UUID("16161616-1616-1616-1616-161616161616"),
            )

        assert event.student_id == existing.id
        assert event.is_repeat_submission is True
        assert (
            Student.objects.for_club(club)
            .filter(is_child=True, guardian_phone="+79174002225", deleted_at__isnull=True)
            .count()
            == 1
        )

    def test_child_submission_same_guardian_different_name_creates_sibling_card(self, club):
        existing = StudentFactory(
            club=club,
            first_name="First Child",
            last_name="",
            is_child=True,
            phone="",
            guardian_phone="+79174002223",
            status=Student.Status.LEAD,
            lead_status=Student.LeadStatus.NEW,
        )

        with patch("django_q.tasks.async_task"):
            event = create_landing_lead_intake(
                club_id=club.id,
                name="Second Child",
                phone="+7 917 400 22 23",
                goal="Ищу тренировки для второго ребенка",
                preferred_format="group",
                is_child=True,
                consent=_consent(),
                source=_source(page="/kids"),
                request_id="req-child-shared-guardian",
                client_ip_hash="iphash",
                user_agent="Mozilla/5.0",
                idempotency_key=UUID("13131313-1313-1313-1313-131313131313"),
            )

        assert event.student_id != existing.id
        assert event.is_repeat_submission is False
        children = Student.objects.for_club(club).filter(
            is_child=True,
            guardian_phone="+79174002223",
            deleted_at__isnull=True,
        )
        assert children.count() == 2
        assert set(children.values_list("first_name", flat=True)) == {"First Child", "Second Child"}
        assert LeadIntakeEvent.objects.for_club(club).count() == 1

    def test_public_child_submissions_same_guardian_different_names_create_two_child_leads(self, club):
        with patch("django_q.tasks.async_task"):
            first_event = create_landing_lead_intake(
                club_id=club.id,
                name="First Child",
                phone="+7 917 400 22 24",
                goal="Ищу тренировки для первого ребенка",
                preferred_format="group",
                is_child=True,
                consent=_consent(),
                source=_source(page="/kids"),
                request_id="req-child-first",
                client_ip_hash="iphash",
                user_agent="Mozilla/5.0",
                idempotency_key=UUID("14141414-1414-1414-1414-141414141414"),
            )
            second_event = create_landing_lead_intake(
                club_id=club.id,
                name="Second Child",
                phone="+7 917 400 22 24",
                goal="Ищу тренировки для второго ребенка",
                preferred_format="group",
                is_child=True,
                consent=_consent(),
                source=_source(page="/kids"),
                request_id="req-child-second",
                client_ip_hash="iphash",
                user_agent="Mozilla/5.0",
                idempotency_key=UUID("15151515-1515-1515-1515-151515151515"),
            )

        assert first_event.student_id != second_event.student_id
        assert first_event.is_repeat_submission is False
        assert second_event.is_repeat_submission is False
        children = Student.objects.for_club(club).filter(
            is_child=True,
            guardian_phone="+79174002224",
            deleted_at__isnull=True,
        )
        assert children.count() == 2
        assert LeadIntakeEvent.objects.for_club(club).count() == 2

    def test_repeat_submission_for_active_student_does_not_reenter_trainer_pool(self, club):
        trainer = TrainerFactory(club=club)
        existing = StudentFactory(
            club=club,
            first_name="Active Existing",
            phone="+79174009901",
            status=Student.Status.ACTIVE,
            lead_status=None,
            assigned_trainer=trainer,
        )

        with patch("django_q.tasks.async_task"):
            event = _submit_public_intake_for_phone(
                club,
                phone="8 (917) 400-99-01",
                idempotency_key=UUID("44444444-4444-4444-4444-444444444444"),
            )

        assert event.student_id == existing.id
        assert event.is_repeat_submission is True
        assert Student.objects.filter(club=club, phone="+79174009901").count() == 1
        existing.refresh_from_db()
        assert existing.status == Student.Status.ACTIVE
        assert existing.lead_status is None
        assert existing.assigned_trainer_id == trainer.id
        assert existing.id not in set(get_leads(club=club, scope="pool").values_list("id", flat=True))
        assert existing.id not in set(
            get_leads(club=club, scope="mine", current_trainer_id=trainer.id).values_list("id", flat=True)
        )

    def test_repeat_submission_for_lost_student_reenters_unassigned_pool(self, club):
        trainer = TrainerFactory(club=club)
        existing = StudentFactory(
            club=club,
            first_name="Lost Existing",
            phone="+79174009902",
            status=Student.Status.LOST,
            lead_status=None,
            assigned_trainer=trainer,
            loss_reason="expensive",
        )

        with patch("django_q.tasks.async_task"):
            event = _submit_public_intake_for_phone(
                club,
                phone="+7 917 400 99 02",
                idempotency_key=UUID("55555555-5555-5555-5555-555555555555"),
            )

        assert event.student_id == existing.id
        assert event.is_repeat_submission is True
        assert Student.objects.filter(club=club, phone="+79174009902").count() == 1
        existing.refresh_from_db()
        assert existing.status == Student.Status.LEAD
        assert existing.lead_status == Student.LeadStatus.NEW
        assert existing.assigned_trainer_id is None
        assert existing.loss_reason is None
        assert existing.id in set(get_leads(club=club, scope="pool").values_list("id", flat=True))
        assert existing.id not in set(
            get_leads(club=club, scope="mine", current_trainer_id=trainer.id).values_list("id", flat=True)
        )

    def test_idempotency_key_returns_existing_event_without_duplicate_task(self, club):
        key = UUID("33333333-3333-3333-3333-333333333333")

        with patch("django_q.tasks.async_task") as async_task:
            first = create_landing_lead_intake(
                club_id=club.id,
                name="First",
                phone="+79170000000",
                goal="Хочу спокойно попробовать с нуля",
                preferred_format="group",
                is_child=False,
                consent=_consent(),
                source=_source(),
                request_id="req-1",
                client_ip_hash="iphash",
                user_agent="Mozilla/5.0",
                idempotency_key=key,
            )
            second = create_landing_lead_intake(
                club_id=club.id,
                name="Second",
                phone="+79170000000",
                goal="Другая цель",
                preferred_format="personal",
                is_child=False,
                consent=_consent(),
                source=_source(),
                request_id="req-2",
                client_ip_hash="iphash",
                user_agent="Mozilla/5.0",
                idempotency_key=key,
            )

        assert second.id == first.id
        assert LeadIntakeEvent.objects.filter(club=club, idempotency_key=key).count() == 1
        assert async_task.call_count == 1

    def test_rejects_missing_personal_data_consent(self, club):
        consent = _consent()
        consent["personal_data"] = False

        with pytest.raises(BusinessLogicError) as exc_info:
            create_landing_lead_intake(
                club_id=club.id,
                name="No Consent",
                phone="+79170000001",
                goal="Хочу попробовать",
                preferred_format="group",
                is_child=False,
                consent=consent,
                source=_source(),
                request_id="req-no-consent",
                client_ip_hash="iphash",
                user_agent="Mozilla/5.0",
                idempotency_key=None,
            )

        assert exc_info.value.code == "consent_required"
        assert LeadIntakeEvent.objects.count() == 0

    def test_rejects_invalid_phone(self, club):
        with pytest.raises(BusinessLogicError) as exc_info:
            create_landing_lead_intake(
                club_id=club.id,
                name="Invalid Phone",
                phone="123",
                goal="Хочу попробовать",
                preferred_format="group",
                is_child=False,
                consent=_consent(),
                source=_source(),
                request_id="req-invalid-phone",
                client_ip_hash="iphash",
                user_agent="Mozilla/5.0",
                idempotency_key=None,
            )

        assert exc_info.value.code == "invalid_phone"
        assert str(exc_info.value) == "Invalid phone"
