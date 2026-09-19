from datetime import datetime

from ninja import Schema


class PushSubscriptionIn(Schema):
    endpoint: str
    key_p256dh: str
    key_auth: str


class PushSubscriptionOut(Schema):
    id: int
    endpoint: str
    created_at: datetime


class UnsubscribeIn(Schema):
    endpoint: str


class MassNotificationIn(Schema):
    text: str
    segment_type: str
    segment_filter: dict = {}


class MassNotificationOut(Schema):
    id: int
    text: str
    segment_type: str
    segment_filter: dict
    recipient_count: int
    sent_by_id: int
    created_at: datetime


class MassNotificationPreviewOut(Schema):
    recipient_count: int


class VapidKeyOut(Schema):
    public_key: str


class NotificationTemplateOut(Schema):
    id: int
    trigger_type: str
    title_template: str
    body_template: str
    is_enabled: bool
    days_before: int | None


class NotificationTemplateUpdate(Schema):
    title_template: str | None = None
    body_template: str | None = None
    is_enabled: bool | None = None
    days_before: int | None = None


class OptOutIn(Schema):
    opt_out: bool  # True = disable, False = re-enable


class NotificationPreferencesIn(Schema):
    disabled_categories: list[str] = []


class NotificationPreferencesOut(Schema):
    disabled_categories: list[str]
