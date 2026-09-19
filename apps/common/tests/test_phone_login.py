import pytest
from django.conf import settings

from apps.common.phone import is_valid_normalized_phone, normalize_phone


def test_allauth_username_login_is_enabled_for_phone_transition():
    assert settings.ACCOUNT_LOGIN_METHODS == {"email", "username"}
    assert "username*" in settings.ACCOUNT_SIGNUP_FIELDS
    assert "email" in settings.ACCOUNT_SIGNUP_FIELDS


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("8 (900) 123-45-67", "+79001234567"),
        ("79001234567", "+79001234567"),
        ("9001234567", "+79001234567"),
        ("+7 (900) 123-45-67", "+79001234567"),
        ("+7 (900) 123+45+67", "+79001234567"),
    ],
)
def test_normalize_phone_for_account_login(raw, expected):
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize(
    ("phone", "expected"),
    [
        ("+79001234567", True),
        ("79001234567", True),
        ("9001234567", True),
        ("+7900", False),
        ("not-a-phone", False),
        ("", False),
    ],
)
def test_is_valid_normalized_phone(phone, expected):
    assert is_valid_normalized_phone(phone) is expected
