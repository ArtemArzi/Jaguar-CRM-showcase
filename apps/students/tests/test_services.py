import io
from datetime import date

import pytest
from django.db import transaction
from django.utils import timezone
from openpyxl import Workbook

from apps.clubs.tests.factories import UserFactory
from apps.common.exceptions import BusinessLogicError
from apps.leads.models import LeadLifecycleEvent
from apps.leads.selectors import get_leads
from apps.retention.models import RetentionTask
from apps.students.duplicates import DuplicateStudentError
from apps.students.models import Student
from apps.students.selectors import get_students
from apps.students.services import (
    add_student_note,
    create_student,
    delete_student,
    import_students_from_excel,
    transition_status,
    update_student,
)
from apps.students.tests.factories import StudentFactory
from apps.trainers.tests.factories import TrainerFactory


@pytest.mark.django_db
class TestCreateStudent:
    def test_create_student(self, club):
        student = create_student(
            club_id=club.id,
            first_name="Ivan",
            last_name="Petrov",
            phone="+79001234567",
        )
        assert student.first_name == "Ivan"
        assert student.last_name == "Petrov"
        assert student.phone == "+79001234567"
        assert student.status == "lead"
        assert student.lead_status == "new"
        assert student.club_id == club.id

    def test_create_student_with_trainer_creates_visible_lead_assignment_event(self, club):
        trainer = TrainerFactory(club=club)

        student = create_student(
            club_id=club.id,
            first_name="Ivan",
            last_name="Petrov",
            phone="+79001234567",
            assigned_trainer_id=trainer.id,
        )

        assert student.status == "lead"
        assert student.lead_status == "new"
        event = LeadLifecycleEvent.objects.get(student=student)
        assert event.event_type == LeadLifecycleEvent.EventType.LEAD_ASSIGNED
        assert event.new_lead_status == "new"
        assert event.new_trainer_id == trainer.id
        assert (
            not RetentionTask.objects.for_club(club)
            .filter(
                student=student,
                task_type=RetentionTask.TaskType.NEW_LEAD,
            )
            .exists()
        )

    def test_create_student_lead_stays_in_leads_without_new_lead_task(self, club):
        TrainerFactory(club=club)

        student = create_student(
            club_id=club.id,
            first_name="Lead",
            last_name="Queue",
            phone="+79001234568",
        )

        assert get_leads(club=club).filter(id=student.id).exists()
        assert (
            not RetentionTask.objects.for_club(club)
            .filter(
                student=student,
                task_type=RetentionTask.TaskType.NEW_LEAD,
            )
            .exists()
        )

    def test_create_student_duplicate_phone(self, club):
        create_student(
            club_id=club.id,
            first_name="Ivan",
            last_name="Petrov",
            phone="+79001234567",
        )
        with pytest.raises(BusinessLogicError) as exc_info:
            create_student(
                club_id=club.id,
                first_name="Petr",
                last_name="Ivanov",
                phone="+79001234567",
            )
        assert exc_info.value.code == "duplicate_phone"

    def test_create_adult_rejects_existing_child_guardian_phone(self, club):
        create_student(
            club_id=club.id,
            first_name="Masha",
            last_name="Petrova",
            phone="+79001234570",
            is_child=True,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            create_student(
                club_id=club.id,
                first_name="Parent",
                last_name="Duplicate",
                phone="+79001234570",
            )

        assert exc_info.value.code == "duplicate_phone"

    def test_create_child_rejects_existing_adult_phone_as_guardian_phone(self, club):
        create_student(
            club_id=club.id,
            first_name="Adult",
            last_name="Existing",
            phone="+79001234570",
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            create_student(
                club_id=club.id,
                first_name="Masha",
                last_name="Petrova",
                phone="+79001234570",
                is_child=True,
            )

        assert exc_info.value.code == "duplicate_phone"

    def test_create_student_rejects_duplicate_phone_after_soft_delete_without_restoring(self, club):
        student = create_student(
            club_id=club.id,
            first_name="Ivan",
            last_name="Petrov",
            phone="+79001234567",
        )
        student.soft_delete()

        with pytest.raises(DuplicateStudentError) as exc_info:
            create_student(
                club_id=club.id,
                first_name="Ivan",
                last_name="Petrov",
                phone="+79001234567",
            )

        assert exc_info.value.code == "duplicate_phone"
        student.refresh_from_db()
        assert student.deleted_at is not None
        assert Student.objects.for_club(club).filter(phone="+79001234567").count() == 1

    def test_create_student_allows_second_child_on_same_guardian_phone(self, club):
        first = create_student(
            club_id=club.id,
            first_name="Masha",
            last_name="Petrova",
            phone="+79001234570",
            is_child=True,
        )
        second = create_student(
            club_id=club.id,
            first_name="Petr",
            last_name="Petrov",
            phone="+79001234570",
            is_child=True,
        )

        assert first.phone == ""
        assert second.phone == ""
        assert first.guardian_phone == "+79001234570"
        assert second.guardian_phone == "+79001234570"

    def test_create_student_rejects_same_child_on_same_guardian_phone(self, club):
        create_student(
            club_id=club.id,
            first_name="Masha",
            last_name="Petrova",
            phone="+79001234570",
            is_child=True,
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            create_student(
                club_id=club.id,
                first_name="Masha",
                last_name="Petrova",
                phone="+79001234570",
                is_child=True,
            )

        assert exc_info.value.code == "duplicate_phone"

    def test_create_student_delegates_duplicate_arbitration_to_shared_identity_service(self, club, monkeypatch):
        from apps.students import services

        calls = []
        original = services.resolve_staff_intake_person_identity

        def recording_arbitration(**kwargs):
            calls.append(kwargs["confirm_distinct_child"])
            return original(**kwargs)

        monkeypatch.setattr(services, "resolve_staff_intake_person_identity", recording_arbitration)

        create_student(
            club_id=club.id,
            first_name="Shared",
            last_name="Lock",
            phone="8 (917) 400-20-40",
        )

        assert calls == [False]

    def test_create_student_allows_same_named_siblings_with_distinct_birth_dates(self, club):
        first = create_student(
            club_id=club.id,
            first_name="Masha",
            last_name="Petrova",
            phone="",
            guardian_phone="+79001234568",
            is_child=True,
            date_of_birth=date(2017, 5, 6),
        )
        sibling = create_student(
            club_id=club.id,
            first_name="Masha",
            last_name="Petrova",
            phone="",
            guardian_phone="+79001234568",
            is_child=True,
            date_of_birth=date(2018, 5, 6),
        )

        assert {first.date_of_birth, sibling.date_of_birth} == {
            date(2017, 5, 6),
            date(2018, 5, 6),
        }

    def test_create_student_maps_same_child_without_birth_date_to_legacy_duplicate(self, club):
        create_student(
            club_id=club.id,
            first_name="Masha",
            last_name="Petrova",
            phone="",
            guardian_phone="+79001234568",
            is_child=True,
        )

        with pytest.raises(DuplicateStudentError) as exc_info:
            create_student(
                club_id=club.id,
                first_name="Masha",
                last_name="Petrova",
                phone="",
                guardian_phone="+79001234568",
                is_child=True,
            )

        assert exc_info.value.code == "duplicate_phone"


@pytest.mark.django_db(transaction=True)
def test_update_student_duplicate_lookup_is_safe_outside_atomic(club):
    student = StudentFactory(club=club, phone="+79001234567")

    updated = update_student(
        club_id=club.id,
        student_id=student.id,
        first_name="Updated",
    )

    assert updated.first_name == "Updated"


@pytest.mark.django_db
class TestTransitionStatus:
    def test_transition_status_valid(self, club):
        student = StudentFactory(club=club, status=Student.Status.LEAD, lead_status=Student.LeadStatus.NEW)
        updated = transition_status(student_id=student.id, club_id=club.id, new_status="trial")
        assert updated.status == Student.Status.TRIAL
        assert updated.lead_status is None
        assert list(get_leads(club=club)) == []

        event = LeadLifecycleEvent.objects.get(student=student)
        assert event.event_type == LeadLifecycleEvent.EventType.STATUS_CHANGED
        assert event.old_lead_status == Student.LeadStatus.NEW
        assert event.new_lead_status == ""

    def test_transition_status_to_lead_restores_lead_status(self, club):
        student = StudentFactory(club=club, status=Student.Status.TRIAL, lead_status=None)

        updated = transition_status(student_id=student.id, club_id=club.id, new_status=Student.Status.LEAD)

        assert updated.status == Student.Status.LEAD
        assert updated.lead_status == Student.LeadStatus.NEW
        event = LeadLifecycleEvent.objects.get(student=student)
        assert event.event_type == LeadLifecycleEvent.EventType.STATUS_CHANGED
        assert event.old_lead_status == ""
        assert event.new_lead_status == Student.LeadStatus.NEW

    def test_transition_status_trial_to_active_records_conversion_and_finalizes(
        self,
        club,
        django_capture_on_commit_callbacks,
        monkeypatch,
    ):
        actor = UserFactory()
        student = StudentFactory(club=club, status=Student.Status.TRIAL, lead_status=None)
        finalizer_calls = []

        def fake_finalize(*, club_id: int, student_id: int):
            finalizer_calls.append({"club_id": club_id, "student_id": student_id})

        monkeypatch.setattr(
            "apps.leads.services._finalize_lead_conversion_side_effects",
            fake_finalize,
        )

        with django_capture_on_commit_callbacks(execute=True):
            updated = transition_status(
                student_id=student.id,
                club_id=club.id,
                new_status=Student.Status.ACTIVE,
                actor_user_id=actor.id,
                source="test_status_transition",
            )
            assert finalizer_calls == []

        assert updated.status == Student.Status.ACTIVE
        assert updated.became_student_at is None
        event = LeadLifecycleEvent.objects.for_club(club).get(
            student=student,
            event_type=LeadLifecycleEvent.EventType.LEAD_CONVERTED,
        )
        assert event.actor_id == actor.id
        assert event.old_lead_status == ""
        assert event.new_lead_status == ""
        assert event.metadata["source"] == "test_status_transition"
        assert finalizer_calls == [{"club_id": club.id, "student_id": student.id}]

    @pytest.mark.django_db(transaction=True)
    def test_transition_status_keeps_facade_finalizer_on_commit_and_skips_outer_rollback(
        self,
        club,
        monkeypatch,
    ):
        student = StudentFactory(club=club, status=Student.Status.TRIAL, lead_status=None)
        finalizer_calls = []

        def fake_finalize(*, club_id: int, student_id: int) -> None:
            finalizer_calls.append({"club_id": club_id, "student_id": student_id})

        monkeypatch.setattr(
            "apps.leads.services._finalize_lead_conversion_side_effects",
            fake_finalize,
        )

        with pytest.raises(RuntimeError, match="rollback outer transaction"):
            with transaction.atomic():
                updated = transition_status(
                    student_id=student.id,
                    club_id=club.id,
                    new_status=Student.Status.ACTIVE,
                    source="test_status_transition_rollback",
                )
                assert updated.status == Student.Status.ACTIVE
                assert finalizer_calls == []
                raise RuntimeError("rollback outer transaction")

        student.refresh_from_db()
        assert student.status == Student.Status.TRIAL
        assert finalizer_calls == []

    def test_transition_status_never_infers_provenance_or_clears_an_existing_fact(self, club):
        student = StudentFactory(club=club, status=Student.Status.TRIAL, lead_status=None)

        # Free-trial lifecycle evidence alone does not make a student.
        assert student.became_student_at is None
        updated = transition_status(
            student_id=student.id,
            club_id=club.id,
            new_status=Student.Status.ACTIVE,
        )
        assert updated.became_student_at is None

        first_became_student_at = timezone.now()
        Student.objects.for_club(club).filter(id=student.id).update(
            became_student_at=first_became_student_at
        )

        transition_status(
            student_id=student.id,
            club_id=club.id,
            new_status=Student.Status.AT_RISK,
        )
        transition_status(
            student_id=student.id,
            club_id=club.id,
            new_status=Student.Status.CHURNED,
        )
        reentered = transition_status(
            student_id=student.id,
            club_id=club.id,
            new_status=Student.Status.ACTIVE,
        )

        assert reentered.became_student_at == first_became_student_at

    def test_transition_status_invalid(self, club):
        student = StudentFactory(club=club, status=Student.Status.LEAD)
        with pytest.raises(BusinessLogicError) as exc_info:
            transition_status(student_id=student.id, club_id=club.id, new_status="churned")
        assert exc_info.value.code == "invalid_transition"


@pytest.mark.django_db
class TestDeleteStudent:
    def test_delete_student_soft_deletes_in_club_scope(self, club):
        student = StudentFactory(club=club)

        delete_student(student_id=student.id, club_id=club.id)

        student.refresh_from_db()
        assert student.deleted_at is not None


def _make_excel_file(rows: list[list]) -> io.BytesIO:
    """Create an in-memory .xlsx file with given rows (first row = header)."""
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


@pytest.mark.django_db
class TestImportStudents:
    def test_import_students_from_excel(self, club):
        file = _make_excel_file(
            [
                ["Name", "Phone"],
                ["Ivan Petrov", "+79001111111"],
                ["Petr Sidorov", "+79002222222"],
            ]
        )
        result = import_students_from_excel(club_id=club.id, file=file)
        assert result.created == 2
        assert result.skipped == 0
        assert result.errors == []
        assert set(
            Student.objects.for_club(club)
            .filter(phone__in={"+79001111111", "+79002222222"})
            .values_list("lead_status", flat=True)
        ) == {Student.LeadStatus.NEW}

    def test_import_students_from_excel_duplicate(self, club):
        StudentFactory(club=club, phone="+79001111111")
        file = _make_excel_file(
            [
                ["Name", "Phone"],
                ["Ivan Petrov", "+79001111111"],
            ]
        )
        result = import_students_from_excel(club_id=club.id, file=file)
        assert result.created == 0
        assert result.skipped == 1
        assert result.errors == []

    def test_import_students_from_excel_skips_existing_guardian_phone(self, club):
        StudentFactory(
            club=club,
            first_name="Child",
            is_child=True,
            phone="",
            guardian_phone="+79001111112",
        )
        file = _make_excel_file(
            [
                ["Name", "Phone"],
                ["Imported Duplicate", "+7 (900) 111-11-12"],
            ]
        )

        result = import_students_from_excel(club_id=club.id, file=file)

        assert result.created == 0
        assert result.skipped == 1
        assert result.errors == []
        assert Student.objects.for_club(club).filter(phone="+79001111112").count() == 0

    def test_import_students_uses_shared_identity_arbitration_for_each_valid_row(self, club, monkeypatch):
        from apps.students import services

        calls = []
        original = services.arbitrate_person_identity

        def recording_arbitration(**kwargs):
            calls.append(kwargs["identity"].phone)
            return original(**kwargs)

        monkeypatch.setattr(services, "arbitrate_person_identity", recording_arbitration)
        file = _make_excel_file(
            [
                ["Name", "Phone"],
                ["One Person", "+79001111113"],
                ["Duplicate Person", "+79001111113"],
            ]
        )

        result = import_students_from_excel(club_id=club.id, file=file)

        assert result.created == 1
        assert result.skipped == 1
        assert calls == ["+79001111113", "+79001111113"]

    def test_import_students_from_excel_invalid_rows(self, club):
        file = _make_excel_file(
            [
                ["Name", "Phone"],
                ["", "+79001111111"],
                ["Ivan Petrov", ""],
            ]
        )
        result = import_students_from_excel(club_id=club.id, file=file)
        assert result.created == 0
        assert len(result.errors) == 2


@pytest.mark.django_db
class TestTenantIsolation:
    def test_get_students_tenant_isolation(self, club, other_club):
        StudentFactory(club=club)
        StudentFactory(club=other_club)
        students = get_students(club=club)
        assert students.count() == 1
        assert students.first().club_id == club.id


@pytest.mark.django_db
class TestStudentNote:
    def test_add_student_note(self, club):
        student = StudentFactory(club=club)
        author = UserFactory()
        note = add_student_note(
            club_id=club.id,
            student_id=student.id,
            author_id=author.id,
            text="Test note text",
        )
        assert note.text == "Test note text"
        assert note.student_id == student.id
        assert note.author_id == author.id
        assert note.club_id == club.id
