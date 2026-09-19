import re
from io import BytesIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from PIL import Image

from apps.clubs.tests.factories import ClubFactory
from apps.common.exceptions import BusinessLogicError
from apps.documents.models import StudentDocument
from apps.documents.selectors import get_document_checklist, get_missing_documents_count
from apps.documents.services import (
    create_document_type,
    deactivate_document_type,
    mark_document_provided,
    update_document_type,
    upload_student_document,
)
from apps.documents.tests.factories import DocumentTypeFactory
from apps.students.tests.factories import StudentFactory


def _image_bytes(image_format: str) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (1, 1), color=(196, 90, 59)).save(buffer, format=image_format)
    return buffer.getvalue()


@pytest.mark.django_db
class TestCreateDocumentType:
    def test_creates_with_all_fields(self, club):
        dt = create_document_type(
            club_id=club.id,
            name="Medical Certificate",
            description="Required medical cert",
            is_required=True,
            scope="children",
        )
        assert dt.name == "Medical Certificate"
        assert dt.description == "Required medical cert"
        assert dt.is_required is True
        assert dt.scope == "children"
        assert dt.club_id == club.id

    def test_creates_with_defaults(self, club):
        dt = create_document_type(club_id=club.id, name="Contract")
        assert dt.is_required is True
        assert dt.scope == "all"
        assert dt.description == ""

    def test_raises_business_error_for_duplicate_active_name(self, club):
        DocumentTypeFactory(club=club, name="Contract")

        with pytest.raises(BusinessLogicError) as exc_info:
            create_document_type(club_id=club.id, name="Contract")

        assert exc_info.value.code == "duplicate_document_type_name"

    def test_rejects_invalid_scope(self, club):
        with pytest.raises(BusinessLogicError) as exc_info:
            create_document_type(club_id=club.id, name="Contract", scope="invalid")

        assert exc_info.value.code == "invalid_scope"


@pytest.mark.django_db
class TestUpdateDocumentType:
    def test_updates_fields(self, club):
        dt = DocumentTypeFactory(club=club, name="Old Name")
        updated = update_document_type(club_id=club.id, document_type_id=dt.id, name="New Name")
        assert updated.name == "New Name"
        assert updated.id == dt.id

    def test_raises_business_error_when_renaming_to_duplicate_active_name(self, club):
        first = DocumentTypeFactory(club=club, name="Contract")
        second = DocumentTypeFactory(club=club, name="Medical Certificate")

        with pytest.raises(BusinessLogicError) as exc_info:
            update_document_type(club_id=club.id, document_type_id=second.id, name=first.name)

        assert exc_info.value.code == "duplicate_document_type_name"

    def test_rejects_invalid_scope(self, club):
        dt = DocumentTypeFactory(club=club)

        with pytest.raises(BusinessLogicError) as exc_info:
            update_document_type(club_id=club.id, document_type_id=dt.id, scope="invalid")

        assert exc_info.value.code == "invalid_scope"


@pytest.mark.django_db
class TestDeactivateDocumentType:
    def test_sets_is_active_false(self, club):
        dt = DocumentTypeFactory(club=club)
        assert dt.is_active is True
        result = deactivate_document_type(club_id=club.id, document_type_id=dt.id)
        assert result.is_active is False


@pytest.mark.django_db
class TestGetDocumentChecklist:
    def test_child_sees_all_and_children_scope(self, club):
        DocumentTypeFactory(club=club, name="Contract", scope="all")
        DocumentTypeFactory(club=club, name="Parental Consent", scope="children")
        DocumentTypeFactory(club=club, name="Adult Waiver", scope="adults")

        child = StudentFactory(club=club, is_child=True)
        checklist = get_document_checklist(club=club, student_id=child.id)

        names = [item["document_type"].name for item in checklist]
        assert "Contract" in names
        assert "Parental Consent" in names
        assert "Adult Waiver" not in names

    def test_adult_sees_all_and_adults_scope(self, club):
        DocumentTypeFactory(club=club, name="Contract", scope="all")
        DocumentTypeFactory(club=club, name="Parental Consent", scope="children")
        DocumentTypeFactory(club=club, name="Adult Waiver", scope="adults")

        adult = StudentFactory(club=club, is_child=False)
        checklist = get_document_checklist(club=club, student_id=adult.id)

        names = [item["document_type"].name for item in checklist]
        assert "Contract" in names
        assert "Adult Waiver" in names
        assert "Parental Consent" not in names

    def test_includes_existing_document_status(self, club):
        dt = DocumentTypeFactory(club=club, name="Contract", scope="all")
        student = StudentFactory(club=club)
        StudentDocument.objects.create(club=club, student=student, document_type=dt, is_provided=True)

        checklist = get_document_checklist(club=club, student_id=student.id)
        assert len(checklist) == 1
        assert checklist[0]["is_provided"] is True

    def test_tenant_isolation(self, club):
        other_club = ClubFactory()
        DocumentTypeFactory(club=club, name="Contract", scope="all")
        DocumentTypeFactory(club=other_club, name="Other Contract", scope="all")

        student = StudentFactory(club=club)
        checklist = get_document_checklist(club=club, student_id=student.id)

        names = [item["document_type"].name for item in checklist]
        assert "Contract" in names
        assert "Other Contract" not in names

    def test_preserves_inactive_historical_provided_document(self, club):
        active = DocumentTypeFactory(club=club, name="Active Contract", scope="all", is_active=True)
        archived = DocumentTypeFactory(club=club, name="Archived Contract", scope="all", is_active=False)
        student = StudentFactory(club=club)
        StudentDocument.objects.create(club=club, student=student, document_type=archived, is_provided=True)

        checklist = get_document_checklist(club=club, student_id=student.id)

        names = [item["document_type"].name for item in checklist]
        assert names == ["Active Contract", "Archived Contract"]
        archived_item = next(item for item in checklist if item["document_type"].id == archived.id)
        assert archived_item["document_type"].is_active is False
        assert archived_item["is_provided"] is True
        assert active.id in [item["document_type"].id for item in checklist]

    def test_preserves_inactive_historical_uploaded_document(self, club):
        archived = DocumentTypeFactory(club=club, name="Archived Passport", scope="all", is_active=False)
        student = StudentFactory(club=club)
        StudentDocument.objects.create(
            club=club,
            student=student,
            document_type=archived,
            is_provided=False,
            file="documents/2026/04/passport.pdf",
        )

        checklist = get_document_checklist(club=club, student_id=student.id)

        assert len(checklist) == 1
        assert checklist[0]["document_type"].id == archived.id
        assert checklist[0]["has_file"] is True

    def test_hides_inactive_document_without_historical_value(self, club):
        archived = DocumentTypeFactory(club=club, name="Archived Contract", scope="all", is_active=False)
        student = StudentFactory(club=club)
        StudentDocument.objects.create(club=club, student=student, document_type=archived, is_provided=False)

        checklist = get_document_checklist(club=club, student_id=student.id)

        assert checklist == []

    def test_preserves_historical_document_after_scope_change(self, club):
        doc_type = DocumentTypeFactory(club=club, name="Consent", scope="all", is_active=True)
        student = StudentFactory(club=club, is_child=True)
        StudentDocument.objects.create(club=club, student=student, document_type=doc_type, is_provided=True)
        update_document_type(club_id=club.id, document_type_id=doc_type.id, scope="adults")

        checklist = get_document_checklist(club=club, student_id=student.id)

        assert len(checklist) == 1
        assert checklist[0]["document_type"].id == doc_type.id

    def test_deleted_student_is_not_accessible(self, club):
        student = StudentFactory(club=club, deleted_at=timezone.now())

        with pytest.raises(StudentFactory._meta.model.DoesNotExist):
            get_document_checklist(club=club, student_id=student.id)


@pytest.mark.django_db
class TestGetMissingDocumentsCount:
    def test_counts_required_not_provided(self, club):
        DocumentTypeFactory(club=club, name="Required Doc", scope="all", is_required=True)
        DocumentTypeFactory(club=club, name="Optional Doc", scope="all", is_required=False)

        student = StudentFactory(club=club)
        count = get_missing_documents_count(club=club, student_id=student.id)
        # 1 required, 1 optional -- only required counts
        assert count == 1

    def test_zero_when_all_provided(self, club):
        dt = DocumentTypeFactory(club=club, name="Contract", scope="all", is_required=True)
        student = StudentFactory(club=club)
        StudentDocument.objects.create(club=club, student=student, document_type=dt, is_provided=True)
        count = get_missing_documents_count(club=club, student_id=student.id)
        assert count == 0

    def test_uploaded_file_counts_as_present_even_if_unmarked(self, club):
        dt = DocumentTypeFactory(club=club, name="Passport", scope="all", is_required=True)
        student = StudentFactory(club=club)
        StudentDocument.objects.create(
            club=club,
            student=student,
            document_type=dt,
            is_provided=False,
            file="documents/2026/04/passport.pdf",
        )

        count = get_missing_documents_count(club=club, student_id=student.id)

        assert count == 0


@pytest.mark.django_db
class TestMarkDocumentProvided:
    def test_creates_student_document(self, club):
        dt = DocumentTypeFactory(club=club)
        student = StudentFactory(club=club)
        sd = mark_document_provided(club_id=club.id, student_id=student.id, document_type_id=dt.id)
        assert sd.is_provided is True
        assert sd.club_id == club.id

    def test_updates_existing_record(self, club):
        dt = DocumentTypeFactory(club=club)
        student = StudentFactory(club=club)
        sd1 = mark_document_provided(
            club_id=club.id,
            student_id=student.id,
            document_type_id=dt.id,
            notes="First",
        )
        sd2 = mark_document_provided(
            club_id=club.id,
            student_id=student.id,
            document_type_id=dt.id,
            notes="Updated",
        )
        assert sd1.id == sd2.id
        sd2.refresh_from_db()
        assert sd2.notes == "Updated"

    def test_rejects_document_type_from_other_club(self, club):
        other_club = ClubFactory()
        dt = DocumentTypeFactory(club=other_club)
        student = StudentFactory(club=club)

        with pytest.raises(BusinessLogicError) as exc_info:
            mark_document_provided(club_id=club.id, student_id=student.id, document_type_id=dt.id)

        assert exc_info.value.code == "invalid_document_type"

    def test_rejects_inactive_document_type(self, club):
        dt = DocumentTypeFactory(club=club, is_active=False)
        student = StudentFactory(club=club)

        with pytest.raises(BusinessLogicError) as exc_info:
            mark_document_provided(club_id=club.id, student_id=student.id, document_type_id=dt.id)

        assert exc_info.value.code == "invalid_document_type"

    def test_rejects_document_type_outside_student_scope(self, club):
        dt = DocumentTypeFactory(club=club, scope="children")
        student = StudentFactory(club=club, is_child=False)

        with pytest.raises(BusinessLogicError) as exc_info:
            mark_document_provided(club_id=club.id, student_id=student.id, document_type_id=dt.id)

        assert exc_info.value.code == "invalid_document_type"

    def test_rejects_deleted_student(self, club):
        dt = DocumentTypeFactory(club=club)
        student = StudentFactory(club=club, deleted_at=timezone.now())

        with pytest.raises(StudentFactory._meta.model.DoesNotExist):
            mark_document_provided(club_id=club.id, student_id=student.id, document_type_id=dt.id)


@pytest.mark.django_db
class TestUploadStudentDocument:
    def test_valid_pdf_upload(self, club):
        dt = DocumentTypeFactory(club=club)
        student = StudentFactory(club=club)
        file = SimpleUploadedFile("test.pdf", b"%PDF-1.4 test content", content_type="application/pdf")
        sd = upload_student_document(club_id=club.id, student_id=student.id, document_type_id=dt.id, file=file)
        assert sd.is_provided is True
        assert sd.file
        assert sd.uploaded_at is not None

    @pytest.mark.parametrize(
        ("filename", "content_type", "image_format"),
        [
            ("test.jpg", "image/jpeg", "JPEG"),
            ("test.png", "image/png", "PNG"),
            ("test.webp", "image/webp", "WEBP"),
        ],
    )
    def test_valid_image_upload(self, club, filename, content_type, image_format):
        dt = DocumentTypeFactory(club=club)
        student = StudentFactory(club=club)
        file = SimpleUploadedFile(filename, _image_bytes(image_format), content_type=content_type)

        sd = upload_student_document(club_id=club.id, student_id=student.id, document_type_id=dt.id, file=file)

        assert sd.is_provided is True
        assert sd.file
        assert sd.uploaded_at is not None

    def test_rejects_html_file_declared_as_pdf(self, club):
        dt = DocumentTypeFactory(club=club)
        student = StudentFactory(club=club)
        file = SimpleUploadedFile(
            "exploit.html",
            b"<html><script>alert('xss')</script></html>",
            content_type="application/pdf",
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            upload_student_document(club_id=club.id, student_id=student.id, document_type_id=dt.id, file=file)

        assert exc_info.value.code == "invalid_file_type"

    def test_rejects_spoofed_image_content(self, club):
        dt = DocumentTypeFactory(club=club)
        student = StudentFactory(club=club)
        file = SimpleUploadedFile(
            "exploit.png",
            b"<html><script>alert('xss')</script></html>",
            content_type="image/png",
        )

        with pytest.raises(BusinessLogicError) as exc_info:
            upload_student_document(club_id=club.id, student_id=student.id, document_type_id=dt.id, file=file)

        assert exc_info.value.code == "invalid_file_type"

    def test_upload_uses_safe_stored_filename(self, club):
        dt = DocumentTypeFactory(club=club)
        student = StudentFactory(club=club)
        file = SimpleUploadedFile("medical report <script>.pdf", b"%PDF-1.4 content", content_type="application/pdf")

        sd = upload_student_document(club_id=club.id, student_id=student.id, document_type_id=dt.id, file=file)

        stored_filename = sd.file.name.rsplit("/", 1)[-1]
        assert re.fullmatch(r"[0-9a-f]{32}\.pdf", stored_filename)

    def test_file_too_large_raises_error(self, club):
        dt = DocumentTypeFactory(club=club)
        student = StudentFactory(club=club)
        # Create a file > 10MB
        large_content = b"x" * (10 * 1024 * 1024 + 1)
        file = SimpleUploadedFile("big.pdf", large_content, content_type="application/pdf")
        with pytest.raises(BusinessLogicError) as exc_info:
            upload_student_document(club_id=club.id, student_id=student.id, document_type_id=dt.id, file=file)
        assert exc_info.value.code == "file_too_large"

    def test_invalid_mime_type_raises_error(self, club):
        dt = DocumentTypeFactory(club=club)
        student = StudentFactory(club=club)
        file = SimpleUploadedFile("test.exe", b"bad content", content_type="application/x-msdownload")
        with pytest.raises(BusinessLogicError) as exc_info:
            upload_student_document(club_id=club.id, student_id=student.id, document_type_id=dt.id, file=file)
        assert exc_info.value.code == "invalid_file_type"

    def test_rejects_inactive_document_type(self, club):
        dt = DocumentTypeFactory(club=club, is_active=False)
        student = StudentFactory(club=club)
        file = SimpleUploadedFile("test.pdf", b"%PDF-1.4 test content", content_type="application/pdf")

        with pytest.raises(BusinessLogicError) as exc_info:
            upload_student_document(club_id=club.id, student_id=student.id, document_type_id=dt.id, file=file)

        assert exc_info.value.code == "invalid_document_type"

    def test_rejects_document_type_outside_student_scope(self, club):
        dt = DocumentTypeFactory(club=club, scope="adults")
        student = StudentFactory(club=club, is_child=True)
        file = SimpleUploadedFile("test.pdf", b"%PDF-1.4 test content", content_type="application/pdf")

        with pytest.raises(BusinessLogicError) as exc_info:
            upload_student_document(club_id=club.id, student_id=student.id, document_type_id=dt.id, file=file)

        assert exc_info.value.code == "invalid_document_type"

    def test_rejects_deleted_student(self, club):
        dt = DocumentTypeFactory(club=club)
        student = StudentFactory(club=club, deleted_at=timezone.now())
        file = SimpleUploadedFile("test.pdf", b"%PDF-1.4 test content", content_type="application/pdf")

        with pytest.raises(StudentFactory._meta.model.DoesNotExist):
            upload_student_document(club_id=club.id, student_id=student.id, document_type_id=dt.id, file=file)
