from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import timedelta

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.attendance.models import KioskDevice
from apps.common.exceptions import BusinessLogicError

logger = logging.getLogger(__name__)

PIN_TTL = timedelta(minutes=10)
PIN_GENERATION_MAX_ATTEMPTS = 20
KIOSK_ACTIVATION_MAX_FAILURES = 5
KIOSK_ACTIVATION_GLOBAL_MAX_FAILURES = 50
KIOSK_ACTIVATION_FAILURE_WINDOW_SECONDS = 300
KIOSK_ACTIVATION_LOCKOUT_SECONDS = 300

_INVALID_PIN_MESSAGE = "Неверный PIN-код"
_LOCKED_MESSAGE = "Слишком много попыток. Попробуйте позже"
_EXPIRED_MESSAGE = "PIN-код истек. Сгенерируйте новый PIN"


@dataclass(frozen=True)
class ActivationAttemptBudget:
    global_attempts: int
    source_attempts: int | None = None


def generate_kiosk_pin(*, club_id: int) -> str:
    """Generate a 6-digit PIN for kiosk activation. Deactivates previous active device."""
    now = timezone.now()
    previous_pending_pins: set[str] = set()

    with transaction.atomic():
        # Deactivate any existing active device for this club (locked to prevent race)
        previous_devices = KioskDevice.objects.select_for_update().filter(club_id=club_id, is_active=True)
        previous_pending_pins = set(previous_devices.exclude(pin_code="").values_list("pin_code", flat=True))
        previous_devices.update(
            is_active=False,
            deactivated_at=now,
            pin_code="",
            pin_expires_at=None,
            activation_failed_attempts=0,
            activation_locked_at=None,
        )

        device = None
        pin = ""
        expires_at = now + PIN_TTL
        for _ in range(PIN_GENERATION_MAX_ATTEMPTS):
            pin = f"{secrets.randbelow(1_000_000):06d}"
            if pin in previous_pending_pins or _active_pending_pin_exists(pin):
                logger.info("kiosk_pin_collision_retry", extra={"club_id": club_id})
                continue
            try:
                with transaction.atomic():
                    device = KioskDevice.objects.create(
                        club_id=club_id,
                        token=secrets.token_hex(32),
                        pin_code=pin,
                        pin_expires_at=expires_at,
                        activation_failed_attempts=0,
                        activation_locked_at=None,
                        is_active=True,
                    )
            except IntegrityError:
                logger.info("kiosk_pin_collision_retry", extra={"club_id": club_id})
                continue
            break

        if device is None:
            logger.error("kiosk_pin_generation_failed", extra={"club_id": club_id})
            raise BusinessLogicError(
                "Не удалось сгенерировать PIN-код. Повторите попытку",
                code="kiosk_pin_generation_failed",
            )

    logger.info(
        "kiosk_pin_generated",
        extra={"club_id": club_id, "device_id": device.id},
    )
    return pin


def activate_kiosk(*, pin: str, throttle_key: str | None = None) -> dict:
    """Activate kiosk by PIN. Returns token + club info. Clears PIN after use.

    Concurrency: locks the device row for the duration of the transaction so
    two simultaneous activations with the same PIN cannot both succeed
    (one would otherwise get the device's token and the other would race
    on the same row).
    """
    attempt_budget = _admit_activation_attempt(throttle_key)

    normalized_pin = (pin or "").strip()
    if len(normalized_pin) != 6 or not normalized_pin.isdigit():
        _record_activation_failure(throttle_key, attempt_budget=attempt_budget)
        raise BusinessLogicError(_INVALID_PIN_MESSAGE, code="invalid_kiosk_pin")

    pin_expired = False
    with transaction.atomic():
        device = (
            KioskDevice.objects
            .select_for_update()
            .select_related("club")
            .filter(pin_code=normalized_pin, is_active=True)
            .order_by("id")
            .first()
        )
        if not device:
            _record_activation_failure(throttle_key, attempt_budget=attempt_budget)
            raise BusinessLogicError(_INVALID_PIN_MESSAGE, code="invalid_kiosk_pin")

        # Re-check after lock — defensive: another tx may have cleared PIN
        if not device.pin_code or not device.is_active:
            _record_activation_failure(throttle_key, attempt_budget=attempt_budget)
            raise BusinessLogicError(_INVALID_PIN_MESSAGE, code="invalid_kiosk_pin")

        now = timezone.now()
        if device.activation_locked_at is not None:
            logger.warning(
                "kiosk_activation_device_locked",
                extra={"club_id": device.club_id, "device_id": device.id},
            )
            raise BusinessLogicError(_LOCKED_MESSAGE, code="kiosk_activation_locked")

        if device.pin_expires_at is None or device.pin_expires_at <= now:
            _expire_device_pin(device=device, now=now)
            _record_activation_failure(throttle_key, attempt_budget=attempt_budget)
            pin_expired = True
        else:
            # Clear PIN so it can't be reused (single-use)
            device.pin_code = ""
            device.pin_expires_at = None
            device.activation_failed_attempts = 0
            device.activation_locked_at = None
            device.save(
                update_fields=[
                    "pin_code",
                    "pin_expires_at",
                    "activation_failed_attempts",
                    "activation_locked_at",
                ]
            )
            _clear_activation_failures(throttle_key)
            _clear_global_activation_failures()

            logger.info("kiosk_activated", extra={"club_id": device.club_id, "device_id": device.id})
            return {
                "token": device.token,
                "club_id": device.club_id,
                "club_name": device.club.name,
            }

    if pin_expired:
        raise BusinessLogicError(_EXPIRED_MESSAGE, code="kiosk_pin_expired")

    raise BusinessLogicError(_INVALID_PIN_MESSAGE, code="invalid_kiosk_pin")


def deactivate_kiosk(*, club_id: int) -> None:
    """Deactivate the active kiosk device for a club."""
    updated = KioskDevice.objects.filter(club_id=club_id, is_active=True).update(
        is_active=False,
        deactivated_at=timezone.now(),
        pin_code="",
        pin_expires_at=None,
        activation_failed_attempts=0,
        activation_locked_at=None,
    )
    if updated:
        logger.info("kiosk_deactivated", extra={"club_id": club_id})


def _active_pending_pin_exists(pin: str) -> bool:
    return KioskDevice.objects.filter(pin_code=pin, is_active=True).exists()


def _expire_device_pin(*, device: KioskDevice, now) -> None:
    device.pin_code = ""
    device.pin_expires_at = None
    device.activation_failed_attempts = 0
    device.activation_locked_at = None
    device.is_active = False
    device.deactivated_at = now
    device.save(
        update_fields=[
            "pin_code",
            "pin_expires_at",
            "activation_failed_attempts",
            "activation_locked_at",
            "is_active",
            "deactivated_at",
        ]
    )
    logger.info(
        "kiosk_pin_expired",
        extra={"club_id": device.club_id, "device_id": device.id},
    )


def _activation_source_digest(throttle_key: str) -> str:
    return hashlib.sha256(throttle_key.encode("utf-8")).hexdigest()


def _activation_cache_key(kind: str, throttle_key: str) -> str:
    return f"kiosk_activation:{kind}:{_activation_source_digest(throttle_key)}"


def _activation_global_cache_key(kind: str) -> str:
    return f"kiosk_activation:global:{kind}"


def _activation_global_locked() -> bool:
    return bool(cache.get(_activation_global_cache_key("locked")))


def _activation_source_locked(throttle_key: str | None) -> bool:
    if not throttle_key:
        return False
    return bool(cache.get(_activation_cache_key("locked", throttle_key)))


def _raise_activation_locked(throttle_key: str | None) -> None:
    if throttle_key:
        logger.warning(
            "kiosk_activation_source_locked",
            extra={"activation_source": _activation_source_digest(throttle_key)[:12]},
        )
    raise BusinessLogicError(_LOCKED_MESSAGE, code="kiosk_activation_locked")


def _increment_cache_counter(key: str, timeout: int) -> int:
    if cache.add(key, 1, timeout):
        return 1
    try:
        return int(cache.incr(key))
    except ValueError:
        cache.set(key, 1, timeout)
        return 1


def _admit_activation_attempt(throttle_key: str | None) -> ActivationAttemptBudget:
    if _activation_global_locked():
        _raise_activation_locked(throttle_key)
    if _activation_source_locked(throttle_key):
        _raise_activation_locked(throttle_key)

    global_attempts = _increment_cache_counter(
        _activation_global_cache_key("attempts"),
        KIOSK_ACTIVATION_FAILURE_WINDOW_SECONDS,
    )
    if global_attempts > KIOSK_ACTIVATION_GLOBAL_MAX_FAILURES:
        _lock_global_activation_budget()
        _raise_activation_locked(throttle_key)

    source_attempts = None
    if throttle_key:
        source_attempts = _increment_cache_counter(
            _activation_cache_key("attempts", throttle_key),
            KIOSK_ACTIVATION_FAILURE_WINDOW_SECONDS,
        )
        if source_attempts > KIOSK_ACTIVATION_MAX_FAILURES:
            _lock_activation_source(throttle_key)
            _raise_activation_locked(throttle_key)

    return ActivationAttemptBudget(
        global_attempts=global_attempts,
        source_attempts=source_attempts,
    )


def _record_activation_failure(
    throttle_key: str | None,
    *,
    attempt_budget: ActivationAttemptBudget,
) -> None:
    _record_global_activation_failure(attempt_budget=attempt_budget)
    if not throttle_key or attempt_budget.source_attempts is None:
        return

    attempts_key = _activation_cache_key("attempts", throttle_key)
    attempts = attempt_budget.source_attempts
    source = _activation_source_digest(throttle_key)[:12]
    logger.info(
        "kiosk_activation_failed",
        extra={"activation_source": source, "attempts": attempts},
    )
    if attempts >= KIOSK_ACTIVATION_MAX_FAILURES:
        _lock_activation_source(throttle_key)
        cache.delete(attempts_key)
        logger.warning(
            "kiosk_activation_source_locked",
            extra={"activation_source": source},
        )


def _lock_activation_source(throttle_key: str) -> None:
    cache.set(
        _activation_cache_key("locked", throttle_key),
        True,
        KIOSK_ACTIVATION_LOCKOUT_SECONDS,
    )


def _lock_global_activation_budget() -> None:
    cache.set(
        _activation_global_cache_key("locked"),
        True,
        KIOSK_ACTIVATION_LOCKOUT_SECONDS,
    )
    cache.delete(_activation_global_cache_key("attempts"))
    logger.warning("kiosk_activation_global_locked")


def _record_global_activation_failure(*, attempt_budget: ActivationAttemptBudget) -> None:
    attempts = attempt_budget.global_attempts
    if attempts >= KIOSK_ACTIVATION_GLOBAL_MAX_FAILURES:
        _lock_global_activation_budget()
    else:
        logger.info("kiosk_activation_global_failed", extra={"attempts": attempts})


def _clear_global_activation_failures() -> None:
    cache.delete(_activation_global_cache_key("attempts"))
    cache.delete(_activation_global_cache_key("locked"))


def _clear_activation_failures(throttle_key: str | None) -> None:
    if not throttle_key:
        return
    cache.delete(_activation_cache_key("attempts", throttle_key))
    cache.delete(_activation_cache_key("locked", throttle_key))
