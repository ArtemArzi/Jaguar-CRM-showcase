import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from ninja.testing import TestClient

from apps.clubs.tests.factories import ClubFactory, UserFactory
from apps.common.tests.helpers import make_auth_params as _auth_params
from apps.documents.models import StudentDocument
from apps.documents.tests.factories import DocumentTypeFactory
from apps.students.tests.factories import StudentFactory
from apps.trainers.tests.factories import TrainerFactory
from config.api import api

client = TestClient(api)


@pytest.fixture(autouse=True)
def _bypass_jwt_auth(bypass_jwt_auth):
    pass


@pytest.mark.django_db
class TestCreateDocumentType:
    def test_create_as_owner(self, club, owner_user):
        response = client.post(
            "/documents/types/",
            json={"name": "Medical Certificate", "scope": "children", "is_required": True},
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "Medical Certificate"
        assert data["scope"] == "children"

    def test_create_permission_denied_for_trainer(self, club, trainer_user):
        response = client.post(
            "/documents/types/",
            json={"name": "Contract"},
            **_auth_params(trainer_user, club, role="trainer"),
        )
        assert response.status_code == 403

    def test_create_duplicate_active_name_returns_400(self, club, owner_user):
        DocumentTypeFactory(club=club, name="Contract")

        response = client.post(
            "/documents/types/",
            json={"name": "Contract"},
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "duplicate_document_type_name"

    def test_create_invalid_scope_returns_400(self, club, owner_user):
        response = client.post(
            "/documents/types/",
            json={"name": "Contract", "scope": "invalid"},
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "invalid_scope"


@pytest.mark.django_db
class TestListDocumentTypes:
    def test_list_as_owner(self, club, owner_user):
        DocumentTypeFactory(club=club, name="Contract")
        DocumentTypeFactory(club=club, name="Medical Cert")
        response = client.get("/documents/types/", **_auth_params(owner_user, club))
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2

    def test_list_excludes_inactive(self, club, owner_user):
        DocumentTypeFactory(club=club, name="Active", is_active=True)
        DocumentTypeFactory(club=club, name="Inactive", is_active=False)
        response = client.get("/documents/types/", **_auth_params(owner_user, club))
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["name"] == "Active"

    def test_tenant_isolation(self, club, owner_user):
        other_club = ClubFactory()
        DocumentTypeFactory(club=club, name="My Doc")
        DocumentTypeFactory(club=other_club, name="Other Doc")
        response = client.get("/documents/types/", **_auth_params(owner_user, club))
        data = response.json()
        assert len(data) == 1
        assert data[0]["name"] == "My Doc"


@pytest.mark.django_db
class TestUpdateDocumentType:
    def test_update_as_owner(self, club, owner_user):
        dt = DocumentTypeFactory(club=club, name="Old Name")
        response = client.patch(
            f"/documents/types/{dt.id}/",
            json={"name": "New Name"},
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        assert response.json()["name"] == "New Name"

    def test_update_duplicate_active_name_returns_400(self, club, owner_user):
        first = DocumentTypeFactory(club=club, name="Contract")
        second = DocumentTypeFactory(club=club, name="Medical Certificate")

        response = client.patch(
            f"/documents/types/{second.id}/",
            json={"name": first.name},
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "duplicate_document_type_name"

    def test_update_invalid_scope_returns_400(self, club, owner_user):
        dt = DocumentTypeFactory(club=club, name="Old Name")

        response = client.patch(
            f"/documents/types/{dt.id}/",
            json={"scope": "invalid"},
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "invalid_scope"


@pytest.mark.django_db
class TestDeleteDocumentType:
    def test_delete_deactivates(self, club, owner_user):
        dt = DocumentTypeFactory(club=club)
        response = client.delete(
            f"/documents/types/{dt.id}/",
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 204
        dt.refresh_from_db()
        assert dt.is_active is False


@pytest.mark.django_db
class TestStudentChecklist:
    def test_get_checklist(self, club, owner_user):
        DocumentTypeFactory(club=club, name="Contract", scope="all")
        DocumentTypeFactory(club=club, name="Parental Consent", scope="children")
        student = StudentFactory(club=club, is_child=True)
        response = client.get(
            f"/documents/students/{student.id}/checklist/",
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        names = [item["document_type"]["name"] for item in data]
        assert "Contract" in names
        assert "Parental Consent" in names

    def test_includes_inactive_historical_document(self, club, owner_user):
        student = StudentFactory(club=club)
        archived = DocumentTypeFactory(club=club, name="Archived Contract", scope="all", is_active=False)
        StudentDocument.objects.create(club=club, student=student, document_type=archived, is_provided=True)

        response = client.get(
            f"/documents/students/{student.id}/checklist/",
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["document_type"]["name"] == "Archived Contract"
        assert data[0]["document_type"]["is_active"] is False
        assert data[0]["is_provided"] is True

    def test_parent_can_get_own_child_checklist(self, club, parent_user):
        child = StudentFactory(club=club, is_child=True, parent_user=parent_user)
        DocumentTypeFactory(club=club, name="Base Contract", scope="all")
        DocumentTypeFactory(club=club, name="Parent Consent", scope="children")
        DocumentTypeFactory(club=club, name="Adult Waiver", scope="adults")

        response = client.get(
            f"/documents/students/{child.id}/checklist/",
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 200
        names = [item["document_type"]["name"] for item in response.json()]
        assert names == ["Base Contract", "Parent Consent"]

    def test_parent_cannot_get_other_parent_child_checklist(self, club, parent_user):
        other_parent = UserFactory()
        other_child = StudentFactory(club=club, is_child=True, parent_user=other_parent)

        response = client.get(
            f"/documents/students/{other_child.id}/checklist/",
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 404

    def test_parent_cannot_get_adult_student_checklist_even_if_linked(self, club, parent_user):
        adult = StudentFactory(club=club, is_child=False, parent_user=parent_user)

        response = client.get(
            f"/documents/students/{adult.id}/checklist/",
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 404

    def test_trainer_can_get_assigned_student_checklist(self, club, trainer_user):
        trainer = TrainerFactory(club=club, user=trainer_user)
        student = StudentFactory(club=club, assigned_trainer=trainer)
        DocumentTypeFactory(club=club, name="Base Contract", scope="all")

        response = client.get(
            f"/documents/students/{student.id}/checklist/",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 200
        assert [item["document_type"]["name"] for item in response.json()] == ["Base Contract"]

    def test_trainer_cannot_get_other_student_checklist(self, club, trainer_user):
        TrainerFactory(club=club, user=trainer_user)
        student = StudentFactory(club=club)
        DocumentTypeFactory(club=club, name="Base Contract", scope="all")

        response = client.get(
            f"/documents/students/{student.id}/checklist/",
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403


@pytest.mark.django_db
class TestMarkDocumentProvided:
    def test_mark_provided(self, club, owner_user):
        dt = DocumentTypeFactory(club=club)
        student = StudentFactory(club=club)
        response = client.post(
            f"/documents/students/{student.id}/mark/",
            json={"document_type_id": dt.id, "is_provided": True},
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["is_provided"] is True

    def test_mark_rejects_inactive_document_type(self, club, owner_user):
        dt = DocumentTypeFactory(club=club, is_active=False)
        student = StudentFactory(club=club)

        response = client.post(
            f"/documents/students/{student.id}/mark/",
            json={"document_type_id": dt.id, "is_provided": True},
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "invalid_document_type"

    def test_mark_rejects_document_type_outside_student_scope(self, club, owner_user):
        dt = DocumentTypeFactory(club=club, scope="children")
        student = StudentFactory(club=club, is_child=False)

        response = client.post(
            f"/documents/students/{student.id}/mark/",
            json={"document_type_id": dt.id, "is_provided": True},
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "invalid_document_type"

    def test_trainer_cannot_mark_other_student_document(self, club, trainer_user):
        TrainerFactory(club=club, user=trainer_user)
        dt = DocumentTypeFactory(club=club)
        student = StudentFactory(club=club)

        response = client.post(
            f"/documents/students/{student.id}/mark/",
            json={"document_type_id": dt.id, "is_provided": True},
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403
        assert not StudentDocument.objects.for_club(club).filter(student=student).exists()


@pytest.mark.django_db
class TestUploadDocument:
    def test_upload_valid_file(self, club, owner_user):
        dt = DocumentTypeFactory(club=club)
        student = StudentFactory(club=club)
        file = SimpleUploadedFile("test.pdf", b"%PDF-1.4 content", content_type="application/pdf")
        response = client.post(
            f"/documents/students/{student.id}/upload/",
            POST={"document_type_id": str(dt.id)},
            FILES={"file": file},
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["is_provided"] is True
        assert data["has_file"] is True

    def test_trainer_cannot_upload_other_student_document(self, club, trainer_user):
        TrainerFactory(club=club, user=trainer_user)
        dt = DocumentTypeFactory(club=club)
        student = StudentFactory(club=club)
        file = SimpleUploadedFile("test.pdf", b"%PDF-1.4 content", content_type="application/pdf")

        response = client.post(
            f"/documents/students/{student.id}/upload/",
            POST={"document_type_id": str(dt.id)},
            FILES={"file": file},
            **_auth_params(trainer_user, club, role="trainer"),
        )

        assert response.status_code == 403
        assert not StudentDocument.objects.for_club(club).filter(student=student).exists()

    def test_upload_preserves_notes_for_staff(self, club, owner_user):
        dt = DocumentTypeFactory(club=club)
        student = StudentFactory(club=club)
        StudentDocument.objects.create(
            club=club,
            student=student,
            document_type=dt,
            is_provided=True,
            notes="internal staff note",
        )
        file = SimpleUploadedFile("test.pdf", b"%PDF-1.4 content", content_type="application/pdf")

        response = client.post(
            f"/documents/students/{student.id}/upload/",
            POST={"document_type_id": str(dt.id)},
            FILES={"file": file},
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 200
        assert response.json()["notes"] == "internal staff note"

    def test_student_upload_hides_existing_notes(self, club, student_user):
        dt = DocumentTypeFactory(club=club)
        student = StudentFactory(club=club, user=student_user)
        StudentDocument.objects.create(
            club=club,
            student=student,
            document_type=dt,
            is_provided=True,
            notes="internal staff note",
        )
        file = SimpleUploadedFile("test.pdf", b"%PDF-1.4 content", content_type="application/pdf")

        response = client.post(
            f"/documents/students/{student.id}/upload/",
            POST={"document_type_id": str(dt.id)},
            FILES={"file": file},
            **_auth_params(student_user, club, role="student"),
        )

        assert response.status_code == 200
        assert response.json()["notes"] == ""

    def test_upload_oversized_file_returns_400(self, club, owner_user):
        dt = DocumentTypeFactory(club=club)
        student = StudentFactory(club=club)
        large_content = b"x" * (10 * 1024 * 1024 + 1)
        file = SimpleUploadedFile("big.pdf", large_content, content_type="application/pdf")
        response = client.post(
            f"/documents/students/{student.id}/upload/",
            POST={"document_type_id": str(dt.id)},
            FILES={"file": file},
            **_auth_params(owner_user, club),
        )
        assert response.status_code == 400
        assert response.json()["code"] == "file_too_large"

    def test_upload_html_file_declared_as_pdf_returns_400(self, club, owner_user):
        dt = DocumentTypeFactory(club=club)
        student = StudentFactory(club=club)
        file = SimpleUploadedFile(
            "exploit.html",
            b"<html><script>alert('xss')</script></html>",
            content_type="application/pdf",
        )

        response = client.post(
            f"/documents/students/{student.id}/upload/",
            POST={"document_type_id": str(dt.id)},
            FILES={"file": file},
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "invalid_file_type"

    def test_upload_rejects_inactive_document_type(self, club, owner_user):
        dt = DocumentTypeFactory(club=club, is_active=False)
        student = StudentFactory(club=club)
        file = SimpleUploadedFile("test.pdf", b"%PDF-1.4 content", content_type="application/pdf")

        response = client.post(
            f"/documents/students/{student.id}/upload/",
            POST={"document_type_id": str(dt.id)},
            FILES={"file": file},
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "invalid_document_type"

    def test_upload_rejects_document_type_outside_student_scope(self, club, owner_user):
        dt = DocumentTypeFactory(club=club, scope="adults")
        student = StudentFactory(club=club, is_child=True)
        file = SimpleUploadedFile("test.pdf", b"%PDF-1.4 content", content_type="application/pdf")

        response = client.post(
            f"/documents/students/{student.id}/upload/",
            POST={"document_type_id": str(dt.id)},
            FILES={"file": file},
            **_auth_params(owner_user, club),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "invalid_document_type"

    def test_parent_can_upload_own_child_document_without_seeing_staff_notes(self, club, parent_user):
        dt = DocumentTypeFactory(club=club)
        child = StudentFactory(club=club, is_child=True, parent_user=parent_user)
        StudentDocument.objects.create(
            club=club,
            student=child,
            document_type=dt,
            is_provided=True,
            notes="internal staff note",
        )
        file = SimpleUploadedFile("consent.pdf", b"%PDF-1.4 content", content_type="application/pdf")

        response = client.post(
            f"/documents/students/{child.id}/upload/",
            POST={"document_type_id": str(dt.id)},
            FILES={"file": file},
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 200
        assert response.json()["has_file"] is True
        assert response.json()["notes"] == ""

    def test_parent_cannot_upload_other_parent_child_document(self, club, parent_user):
        other_parent = UserFactory()
        child = StudentFactory(club=club, is_child=True, parent_user=other_parent)
        dt = DocumentTypeFactory(club=club)
        file = SimpleUploadedFile("consent.pdf", b"%PDF-1.4 content", content_type="application/pdf")

        response = client.post(
            f"/documents/students/{child.id}/upload/",
            POST={"document_type_id": str(dt.id)},
            FILES={"file": file},
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 404

    def test_parent_cannot_upload_adult_student_document_even_if_linked(self, club, parent_user):
        adult = StudentFactory(club=club, is_child=False, parent_user=parent_user)
        dt = DocumentTypeFactory(club=club)
        file = SimpleUploadedFile("consent.pdf", b"%PDF-1.4 content", content_type="application/pdf")

        response = client.post(
            f"/documents/students/{adult.id}/upload/",
            POST={"document_type_id": str(dt.id)},
            FILES={"file": file},
            **_auth_params(parent_user, club, role="parent"),
        )

        assert response.status_code == 404

    def test_non_owner_on_type_crud_returns_403(self, club, student_user):
        response = client.post(
            "/documents/types/",
            json={"name": "Contract"},
            **_auth_params(student_user, club, role="student"),
        )
        assert response.status_code == 403
