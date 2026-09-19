from __future__ import annotations

import re

NORMALIZED_PHONE_RE = re.compile(r"^\+?\d{10,15}$")


def normalize_phone(phone: str) -> str:
    """Normalize local RU-style phone input to the app's stored phone shape."""
    raw = str(phone or "").strip()
    has_leading_plus = raw.startswith("+")
    digits = re.sub(r"\D", "", raw)
    normalized = f"+{digits}" if has_leading_plus and digits else digits
    if normalized.startswith("8") and len(normalized) == 11:
        normalized = "+7" + normalized[1:]
    elif normalized.startswith("7") and len(normalized) == 11:
        normalized = "+" + normalized
    elif not normalized.startswith("+") and len(normalized) == 10:
        normalized = "+7" + normalized
    return normalized


def is_valid_normalized_phone(phone: str) -> bool:
    return bool(NORMALIZED_PHONE_RE.fullmatch(phone or ""))
