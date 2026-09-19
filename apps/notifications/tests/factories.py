import factory
from django.utils import timezone

from apps.clubs.tests.factories import ClubFactory, UserFactory
from apps.notifications.models import (
    MassNotification,
    NotificationTemplate,
    PushSubscription,
    SentNotification,
)


class PushSubscriptionFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = PushSubscription

    user = factory.SubFactory(UserFactory)
    endpoint = factory.Sequence(lambda n: f"https://push.example.com/sub/{n}")
    key_p256dh = "test_p256dh"
    key_auth = "test_auth"


class MassNotificationFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = MassNotification

    club = factory.SubFactory(ClubFactory)
    text = "Test notification"
    segment_type = "club"
    segment_filter = {}
    sent_by = factory.SubFactory(UserFactory)
    recipient_count = 0


class NotificationTemplateFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = NotificationTemplate

    club = factory.SubFactory(ClubFactory)
    trigger_type = NotificationTemplate.TriggerType.SUB_EXPIRY_7D
    title_template = "{name}, напоминание"
    body_template = "{name}, ваш абонемент истекает через {days} дней"
    is_enabled = True
    days_before = 7


class SentNotificationFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = SentNotification

    club = factory.SubFactory(ClubFactory)
    student = factory.SubFactory(
        "apps.students.tests.factories.StudentFactory",
        club=factory.SelfAttribute("..club"),
    )
    notification_type = "sub_expiry_7d"
    sent_date = factory.LazyFunction(lambda: timezone.now().date())
