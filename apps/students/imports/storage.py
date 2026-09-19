"""Opaque private files; authorized adapters alone may expose their contents."""

import os
import re
import uuid
from pathlib import Path

from django.conf import settings

from apps.common.exceptions import BusinessLogicError


def private_root() -> Path:
    root = Path(getattr(settings, "STUDENT_IMPORT_PRIVATE_ROOT", settings.BASE_DIR / "local-private/student-imports"))
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink():
        raise BusinessLogicError("Приватное хранилище недоступно.", code="import_storage_unavailable")
    root.chmod(0o700)
    return root.resolve()


def private_path(*, name: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}\.(xlsx|json)", name):
        raise BusinessLogicError("Файл недоступен.", code="target_not_available")
    path = private_root() / name
    if path.is_symlink():
        raise BusinessLogicError("Файл недоступен.", code="target_not_available")
    return path


def save_private(*, content: bytes, suffix: str) -> str:
    if suffix not in {"xlsx", "json"}:
        raise ValueError("Unsupported private file format")
    name = f"{uuid.uuid4().hex}.{suffix}"
    path = private_path(name=name)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(content)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return name
