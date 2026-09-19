from pathlib import Path

import pytest
from django.conf import settings
from django.http import HttpResponse
from django.urls import path

from config.urls import _mark_patterns_public


def test_mark_patterns_public_marks_callbacks_as_login_not_required():
    def media_file_view(request):
        return HttpResponse("ok")

    patterns = [path("media/test.txt", media_file_view)]

    _mark_patterns_public(patterns)

    assert getattr(patterns[0].callback, "login_required", True) is False


@pytest.mark.django_db
def test_debug_document_media_is_not_public_for_anonymous_client(client):
    media_root = Path(settings.MEDIA_ROOT)
    document_dir = media_root / "documents" / "2026" / "06"
    document_dir.mkdir(parents=True, exist_ok=True)
    media_file = document_dir / "private-document.pdf"
    media_file.write_bytes(b"%PDF-1.4 private")

    try:
        response = client.get(f"{settings.MEDIA_URL}documents/2026/06/private-document.pdf")
    finally:
        media_file.unlink(missing_ok=True)

    assert response.status_code == 404


@pytest.mark.django_db
def test_debug_club_logo_media_remains_public_for_anonymous_client(client):
    media_root = Path(settings.MEDIA_ROOT)
    logo_dir = media_root / "club_logos"
    logo_dir.mkdir(parents=True, exist_ok=True)
    media_file = logo_dir / "public-logo.png"
    media_file.write_bytes(b"public-logo")

    try:
        response = client.get(f"{settings.MEDIA_URL}club_logos/public-logo.png")
    finally:
        media_file.unlink(missing_ok=True)

    assert response.status_code == 200
    assert b"".join(response.streaming_content) == b"public-logo"
