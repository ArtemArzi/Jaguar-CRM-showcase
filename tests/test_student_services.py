import pytest

from apps.clubs.tests.factories import ClubFactory
from apps.common.exceptions import BusinessLogicError
from apps.students.services import _normalize_phone, create_student


@pytest.fixture
def club(db):
    return ClubFactory()


class TestNormalizePhone:
    def test_8_prefix(self):
        assert _normalize_phone("89991234567") == "+79991234567"

    def test_already_plus7(self):
        assert _normalize_phone("+79991234567") == "+79991234567"

    def test_strips_formatting(self):
        assert _normalize_phone("+7 (999) 123-45-67") == "+79991234567"

    def test_7_without_plus(self):
        assert _normalize_phone("79991234567") == "+79991234567"

    def test_10_digits(self):
        assert _normalize_phone("9991234567") == "+79991234567"


@pytest.mark.django_db
class TestCreateStudentPhoneNormalization:
    def test_create_student_normalizes_phone(self, club):
        student = create_student(
            club_id=club.id,
            first_name="Ivan",
            last_name="Ivanov",
            phone="89991234567",
        )
        assert student.phone == "+79991234567"

    def test_duplicate_phone_caught_after_normalization(self, club):
        create_student(
            club_id=club.id,
            first_name="Ivan",
            last_name="Ivanov",
            phone="+79991234567",
        )
        with pytest.raises(BusinessLogicError, match="duplicate_phone|already exists|уже существует"):
            create_student(
                club_id=club.id,
                first_name="Petr",
                last_name="Petrov",
                phone="89991234567",
            )
