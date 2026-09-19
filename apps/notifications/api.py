from django.conf import settings
from ninja import Router
from ninja.errors import HttpError
from ninja.pagination import LimitOffsetPagination, paginate

from apps.clubs.models import ClubMembership
from apps.common.auth import TenantJWTOrSessionAuth
from apps.common.permissions import role_required
from apps.common.schemas import schema_sent_fields
from apps.notifications.schemas import (
    MassNotificationIn,
    MassNotificationOut,
    MassNotificationPreviewOut,
    NotificationPreferencesIn,
    NotificationPreferencesOut,
    NotificationTemplateOut,
    NotificationTemplateUpdate,
    OptOutIn,
    PushSubscriptionIn,
    PushSubscriptionOut,
    UnsubscribeIn,
    VapidKeyOut,
)
from apps.notifications.selectors import validate_mass_notification_segment
from apps.notifications.services import (
    get_mass_notification_recipient_ids,
    send_mass_notification,
    subscribe_device,
    unsubscribe_device,
    update_notification_template,
)

router = Router(tags=["notifications"])
push_device_auth = TenantJWTOrSessionAuth()
_NOTIFICATION_TEMPLATE_UPDATE_FIELDS = ("title_template", "body_template", "is_enabled", "days_before")


@router.post("/subscribe/", auth=push_device_auth, response={201: PushSubscriptionOut})
def subscribe_endpoint(request, payload: PushSubscriptionIn):
    sub = subscribe_device(
        user_id=request.user.id,
        endpoint=payload.endpoint,
        key_p256dh=payload.key_p256dh,
        key_auth=payload.key_auth,
    )
    return 201, sub


@router.post("/unsubscribe/", auth=push_device_auth, response={204: None})
def unsubscribe_endpoint(request, payload: UnsubscribeIn):
    unsubscribe_device(user_id=request.user.id, endpoint=payload.endpoint)
    return 204, None


@router.post("/mass/", response={201: MassNotificationOut})
@role_required("owner", "admin", "trainer")
def send_mass_notification_endpoint(request, payload: MassNotificationIn):
    segment_filter = validate_mass_notification_segment(
        club_id=request.club.id,
        segment_type=payload.segment_type,
        segment_filter=payload.segment_filter,
    )
    # Trainer restriction: can only send to own group
    if request._membership.role == ClubMembership.Role.TRAINER:
        if payload.segment_type != "group":
            raise HttpError(403, "Trainers can only send notifications to their own group")
        _validate_trainer_owns_schedule(request, segment_filter)

    notif = send_mass_notification(
        club_id=request.club.id,
        text=payload.text,
        segment_type=payload.segment_type,
        segment_filter=segment_filter,
        sent_by_id=request.user.id,
    )
    return 201, notif


@router.post("/mass/preview/", response=MassNotificationPreviewOut)
@role_required("owner", "admin", "trainer")
def preview_mass_notification(request, payload: MassNotificationIn):
    segment_filter = validate_mass_notification_segment(
        club_id=request.club.id,
        segment_type=payload.segment_type,
        segment_filter=payload.segment_filter,
    )
    if request._membership.role == ClubMembership.Role.TRAINER:
        if payload.segment_type != "group":
            raise HttpError(403, "Trainers can only preview their own group")
        _validate_trainer_owns_schedule(request, segment_filter)

    user_ids = get_mass_notification_recipient_ids(
        club_id=request.club.id,
        segment_type=payload.segment_type,
        segment_filter=segment_filter,
    )
    return {"recipient_count": len(user_ids)}


@router.get("/mass/", response=list[MassNotificationOut])
@role_required("owner", "admin")
@paginate(LimitOffsetPagination)
def list_mass_notifications(request):
    from apps.notifications.models import MassNotification

    return MassNotification.objects.for_club(request.club).order_by("-created_at")


@router.get("/vapid-key/", auth=None, response=VapidKeyOut)
def get_vapid_key(request):
    return {"public_key": settings.VAPID_PUBLIC_KEY}


def _validate_trainer_owns_schedule(request, segment_filter: dict) -> None:
    schedule_id = segment_filter.get("schedule_id")
    if not schedule_id:
        raise HttpError(400, "schedule_id required for group segment")

    from apps.attendance.models import Schedule
    from apps.trainers.models import Trainer

    trainer = Trainer.objects.filter(
        club=request.club,
        user=request.user,
        is_active=True,
    ).first()
    if not trainer:
        raise HttpError(403, "No active trainer profile found")

    schedule = (
        Schedule.objects.for_club(request.club)
        .filter(
            id=schedule_id,
            trainer=trainer,
        )
        .first()
    )
    if not schedule:
        raise HttpError(403, "You can only send to your own group")


# --- Notification Templates (owner only) ---


@router.get("/templates/", response=list[NotificationTemplateOut])
@role_required("owner", "admin")
def list_templates(request):
    from apps.notifications.models import NotificationTemplate

    return NotificationTemplate.objects.for_club(request.club).order_by("trigger_type")


@router.patch("/templates/{template_id}/", response=NotificationTemplateOut)
@role_required("owner", "admin")
def update_template(request, template_id: int, data: NotificationTemplateUpdate):
    return update_notification_template(
        template_id=template_id,
        club_id=request.club.id,
        **schema_sent_fields(data, _NOTIFICATION_TEMPLATE_UPDATE_FIELDS),
    )


# --- Student opt-out ---


@router.post("/opt-out/")
def opt_out(request, data: OptOutIn):
    from apps.notifications.models import PushSubscription

    PushSubscription.objects.filter(user=request.user).update(is_active=not data.opt_out)
    return {"status": "ok", "is_active": not data.opt_out}


# --- Category-based notification preferences ---


@router.get("/preferences/", response=NotificationPreferencesOut)
def get_preferences(request):
    from apps.notifications.models import NotificationPreference

    pref, _ = NotificationPreference.objects.get_or_create(user=request.user)
    return pref


@router.put("/preferences/", response=NotificationPreferencesOut)
def update_preferences(request, data: NotificationPreferencesIn):
    from apps.notifications.models import NotificationPreference

    pref, _ = NotificationPreference.objects.update_or_create(
        user=request.user,
        defaults={"disabled_categories": data.disabled_categories},
    )
    return pref
