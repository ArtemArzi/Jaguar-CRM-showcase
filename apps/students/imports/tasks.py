"""Bounded private-file retention; accepted domain evidence is never deleted."""

import re
from datetime import timedelta

from django.utils import timezone

from apps.students.imports.storage import private_root
from apps.students.models import OpeningImportBatch


def cleanup_expired_import_files():
    """Cross-club maintenance job. Safe IDs/counts only; not a user download API."""
    cutoff = (timezone.now() - timedelta(days=7)).timestamp()
    active_files = set()
    for source, prepared in OpeningImportBatch.objects.unscoped().filter(status="applying").values_list(
        "source_file", "prepared_file",
    ):
        active_files.update((source, prepared))
    removed = 0
    for path in private_root().iterdir():
        if removed >= 1000:
            break
        if (
            re.fullmatch(r"[0-9a-f]{32}\.(xlsx|json)", path.name)
            and path.name not in active_files and not path.is_symlink()
            and path.is_file() and path.stat().st_mtime <= cutoff
        ):
            path.unlink(missing_ok=True)
            removed += 1
    return {"removed_files": removed}
