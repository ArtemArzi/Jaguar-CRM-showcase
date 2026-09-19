from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.billing.models import Subscription
from apps.billing.tests.factories import SubscriptionFactory, TariffFactory, TrainingTypeFactory
from apps.clubs.tests.factories import (
    ClubFactory,
    ClubMembershipFactory,
    LocationFactory,
    UserFactory,
)
from apps.notifications.models import NotificationPreference, NotificationTemplate, SentNotification
from apps.notifications.tests.factories import (
    NotificationTemplateFactory,
    PushSubscriptionFactory,
    SentNotificationFactory,
)
from apps.students.tests.factories import StudentFactory


@pytest.fixture
def setup_club_with_student(club):
    """Create club with student, user, membership, subscription, template, push sub."""
    user = UserFactory()
    student = StudentFactory(club=club, status="active", user=user)
    ClubMembershipFactory(user=user, club=club, role="student")
    push_sub = PushSubscriptionFactory(user=user)
    return {
        "club": club,
        "student": student,
        "user": user,
        "push_sub": push_sub,
    }


@pytest.mark.django_db
class TestCheckSubscriptionExpiry:
    @patch("apps.notifications.services.async_task")
    def test_check_subscription_expiry_sends_7d_push(self, mock_async, setup_club_with_student):
        data = setup_club_with_student
        tt = TrainingTypeFactory(club=data["club"])
        tariff = TariffFactory(club=data["club"], training_type=tt)
        SubscriptionFactory(
            club=data["club"],
            student=data["student"],
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            expires_at=timezone.now() + timedelta(days=7),
        )
        NotificationTemplateFactory(
            club=data["club"],
            trigger_type=NotificationTemplate.TriggerType.SUB_EXPIRY_7D,
            days_before=7,
        )

        from apps.notifications.tasks import check_subscription_expiry

        result = check_subscription_expiry()
        assert result["notifications_sent"] >= 1
        assert SentNotification.objects.filter(
            club=data["club"],
            student=data["student"],
            notification_type=NotificationTemplate.TriggerType.SUB_EXPIRY_7D,
        ).exists()

    @patch("apps.notifications.services.async_task")
    def test_check_subscription_expiry_sends_3d_push(self, mock_async, setup_club_with_student):
        data = setup_club_with_student
        tt = TrainingTypeFactory(club=data["club"])
        tariff = TariffFactory(club=data["club"], training_type=tt)
        SubscriptionFactory(
            club=data["club"],
            student=data["student"],
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            expires_at=timezone.now() + timedelta(days=3),
        )
        NotificationTemplateFactory(
            club=data["club"],
            trigger_type=NotificationTemplate.TriggerType.SUB_EXPIRY_3D,
            days_before=3,
        )

        from apps.notifications.tasks import check_subscription_expiry

        result = check_subscription_expiry()
        assert result["notifications_sent"] >= 1

    @patch("apps.notifications.services.async_task")
    def test_check_subscription_expiry_sends_1d_push(self, mock_async, setup_club_with_student):
        data = setup_club_with_student
        tt = TrainingTypeFactory(club=data["club"])
        tariff = TariffFactory(club=data["club"], training_type=tt)
        SubscriptionFactory(
            club=data["club"],
            student=data["student"],
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            expires_at=timezone.now() + timedelta(days=1),
        )
        NotificationTemplateFactory(
            club=data["club"],
            trigger_type=NotificationTemplate.TriggerType.SUB_EXPIRY_1D,
            days_before=1,
        )

        from apps.notifications.tasks import check_subscription_expiry

        result = check_subscription_expiry()
        assert result["notifications_sent"] >= 1

    @patch("apps.notifications.services.async_task")
    def test_expiry_push_dedup(self, mock_async, setup_club_with_student):
        """Same student+type+day does not send twice."""
        data = setup_club_with_student
        tt = TrainingTypeFactory(club=data["club"])
        tariff = TariffFactory(club=data["club"], training_type=tt)
        SubscriptionFactory(
            club=data["club"],
            student=data["student"],
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            expires_at=timezone.now() + timedelta(days=7),
        )
        NotificationTemplateFactory(
            club=data["club"],
            trigger_type=NotificationTemplate.TriggerType.SUB_EXPIRY_7D,
            days_before=7,
        )
        # Pre-create a SentNotification for today
        SentNotificationFactory(
            club=data["club"],
            student=data["student"],
            notification_type=NotificationTemplate.TriggerType.SUB_EXPIRY_7D,
            sent_date=timezone.now().date(),
        )

        from apps.notifications.tasks import check_subscription_expiry

        result = check_subscription_expiry()
        assert result["notifications_sent"] == 0

    @patch("apps.notifications.services.async_task")
    def test_expiry_push_disabled_template(self, mock_async, setup_club_with_student):
        """Template with is_enabled=False skips send."""
        data = setup_club_with_student
        tt = TrainingTypeFactory(club=data["club"])
        tariff = TariffFactory(club=data["club"], training_type=tt)
        SubscriptionFactory(
            club=data["club"],
            student=data["student"],
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            expires_at=timezone.now() + timedelta(days=7),
        )
        NotificationTemplateFactory(
            club=data["club"],
            trigger_type=NotificationTemplate.TriggerType.SUB_EXPIRY_7D,
            days_before=7,
            is_enabled=False,
        )

        from apps.notifications.tasks import check_subscription_expiry

        result = check_subscription_expiry()
        assert result["notifications_sent"] == 0

    @patch("apps.notifications.services.async_task")
    def test_expiry_push_custom_days(self, mock_async, setup_club_with_student):
        """Template with days_before=10 triggers at 10 days instead of default 7."""
        data = setup_club_with_student
        tt = TrainingTypeFactory(club=data["club"])
        tariff = TariffFactory(club=data["club"], training_type=tt)
        SubscriptionFactory(
            club=data["club"],
            student=data["student"],
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            expires_at=timezone.now() + timedelta(days=10),
        )
        NotificationTemplateFactory(
            club=data["club"],
            trigger_type=NotificationTemplate.TriggerType.SUB_EXPIRY_7D,
            days_before=10,
        )

        from apps.notifications.tasks import check_subscription_expiry

        result = check_subscription_expiry()
        assert result["notifications_sent"] >= 1

    @patch("apps.notifications.services.async_task")
    def test_frozen_sub_no_expiry_push(self, mock_async, setup_club_with_student):
        """Frozen subscription does not trigger expiry push."""
        data = setup_club_with_student
        tt = TrainingTypeFactory(club=data["club"])
        tariff = TariffFactory(club=data["club"], training_type=tt)
        SubscriptionFactory(
            club=data["club"],
            student=data["student"],
            tariff=tariff,
            status=Subscription.Status.FROZEN,
            expires_at=timezone.now() + timedelta(days=7),
        )
        NotificationTemplateFactory(
            club=data["club"],
            trigger_type=NotificationTemplate.TriggerType.SUB_EXPIRY_7D,
            days_before=7,
        )

        from apps.notifications.tasks import check_subscription_expiry

        result = check_subscription_expiry()
        assert result["notifications_sent"] == 0

    @patch("apps.notifications.services.async_task")
    def test_daily_task_processes_all_clubs(self, mock_async):
        """check_subscription_expiry iterates active clubs."""
        club1 = ClubFactory()
        club2 = ClubFactory()
        user1 = UserFactory()
        user2 = UserFactory()
        student1 = StudentFactory(club=club1, user=user1)
        student2 = StudentFactory(club=club2, user=user2)
        ClubMembershipFactory(user=user1, club=club1, role="student")
        ClubMembershipFactory(user=user2, club=club2, role="student")
        PushSubscriptionFactory(user=user1)
        PushSubscriptionFactory(user=user2)

        tt1 = TrainingTypeFactory(club=club1)
        tt2 = TrainingTypeFactory(club=club2)
        tariff1 = TariffFactory(club=club1, training_type=tt1)
        tariff2 = TariffFactory(club=club2, training_type=tt2)
        SubscriptionFactory(
            club=club1,
            student=student1,
            tariff=tariff1,
            status=Subscription.Status.ACTIVE,
            expires_at=timezone.now() + timedelta(days=7),
        )
        SubscriptionFactory(
            club=club2,
            student=student2,
            tariff=tariff2,
            status=Subscription.Status.ACTIVE,
            expires_at=timezone.now() + timedelta(days=7),
        )
        NotificationTemplateFactory(
            club=club1,
            trigger_type=NotificationTemplate.TriggerType.SUB_EXPIRY_7D,
            days_before=7,
        )
        NotificationTemplateFactory(
            club=club2,
            trigger_type=NotificationTemplate.TriggerType.SUB_EXPIRY_7D,
            days_before=7,
        )

        from apps.notifications.tasks import check_subscription_expiry

        result = check_subscription_expiry()
        assert result["clubs_checked"] >= 2
        assert result["notifications_sent"] >= 2

    @patch("apps.notifications.services.async_task")
    def test_expiry_push_tenant_isolation(self, mock_async):
        """Push only for own club subscriptions."""
        club1 = ClubFactory()
        club2 = ClubFactory()
        user1 = UserFactory()
        student1 = StudentFactory(club=club1, user=user1)
        student2 = StudentFactory(club=club2)
        ClubMembershipFactory(user=user1, club=club1, role="student")
        PushSubscriptionFactory(user=user1)

        tt1 = TrainingTypeFactory(club=club1)
        tariff1 = TariffFactory(club=club1, training_type=tt1)
        SubscriptionFactory(
            club=club1,
            student=student1,
            tariff=tariff1,
            status=Subscription.Status.ACTIVE,
            expires_at=timezone.now() + timedelta(days=7),
        )
        NotificationTemplateFactory(
            club=club1,
            trigger_type=NotificationTemplate.TriggerType.SUB_EXPIRY_7D,
            days_before=7,
        )
        # Club2 has no template, so club2 student shouldn't get notified
        tt2 = TrainingTypeFactory(club=club2)
        tariff2 = TariffFactory(club=club2, training_type=tt2)
        SubscriptionFactory(
            club=club2,
            student=student2,
            tariff=tariff2,
            status=Subscription.Status.ACTIVE,
            expires_at=timezone.now() + timedelta(days=7),
        )

        from apps.notifications.tasks import check_subscription_expiry

        check_subscription_expiry()
        # Only club1 should have a SentNotification
        assert SentNotification.objects.filter(club=club1).count() == 1
        assert SentNotification.objects.filter(club=club2).count() == 0


@pytest.mark.django_db
class TestCheckSubscriptionExpiryParentPush:
    @patch("apps.notifications.services.send_push_to_user")
    @patch("apps.notifications.services.async_task")
    def test_check_subscription_expiry_sends_parent_push(self, mock_async, mock_parent_push, club):
        """When child's subscription is expiring, parent_user receives push."""
        parent = UserFactory()
        child = StudentFactory(club=club, is_child=True, parent_user=parent)
        user = UserFactory()
        ClubMembershipFactory(user=user, club=club, role="student")
        PushSubscriptionFactory(user=user)
        PushSubscriptionFactory(user=parent)

        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(club=club, training_type=tt)
        SubscriptionFactory(
            club=club,
            student=child,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            expires_at=timezone.now() + timedelta(days=3),
        )
        NotificationTemplateFactory(
            club=club,
            trigger_type=NotificationTemplate.TriggerType.SUB_EXPIRY_3D,
            days_before=3,
        )

        from apps.notifications.tasks import check_subscription_expiry

        check_subscription_expiry()

        mock_parent_push.assert_called_once()
        call_kwargs = mock_parent_push.call_args[1]
        assert call_kwargs["user_id"] == parent.id
        assert call_kwargs["url"] == f"/parent/child/{child.id}"
        assert child.first_name in call_kwargs["body"]

    @patch("apps.notifications.tasks.send_parent_notification")
    def test_no_parent_push_for_non_child(self, mock_parent_push, setup_club_with_student):
        """Adult student does not trigger parent push."""
        data = setup_club_with_student
        tt = TrainingTypeFactory(club=data["club"])
        tariff = TariffFactory(club=data["club"], training_type=tt)
        SubscriptionFactory(
            club=data["club"],
            student=data["student"],
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            expires_at=timezone.now() + timedelta(days=3),
        )
        NotificationTemplateFactory(
            club=data["club"],
            trigger_type=NotificationTemplate.TriggerType.SUB_EXPIRY_3D,
            days_before=3,
        )

        from apps.notifications.tasks import check_subscription_expiry

        check_subscription_expiry()

        mock_parent_push.assert_not_called()

    @patch("apps.notifications.services.async_task")
    def test_parent_subscription_expiry_respects_subscription_opt_out(self, mock_async, club):
        parent = UserFactory()
        NotificationPreference.objects.create(
            user=parent,
            disabled_categories=["subscription_alerts"],
        )
        PushSubscriptionFactory(user=parent)
        child = StudentFactory(club=club, is_child=True, parent_user=parent)

        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(club=club, training_type=tt)
        SubscriptionFactory(
            club=club,
            student=child,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            expires_at=timezone.now() + timedelta(days=3),
        )
        NotificationTemplateFactory(
            club=club,
            trigger_type=NotificationTemplate.TriggerType.SUB_EXPIRY_3D,
            days_before=3,
        )

        from apps.notifications.tasks import check_subscription_expiry

        check_subscription_expiry()

        mock_async.assert_not_called()
        assert not SentNotification.objects.filter(
            club=club,
            student=child,
            notification_type=NotificationTemplate.TriggerType.PARENT_SUB_EXPIRY,
        ).exists()

    @patch("apps.notifications.services.async_task")
    def test_parent_subscription_expiry_is_idempotent_per_child_per_day(self, mock_async, club):
        parent = UserFactory()
        PushSubscriptionFactory(user=parent)
        child = StudentFactory(club=club, is_child=True, parent_user=parent)

        tt = TrainingTypeFactory(club=club)
        tariff = TariffFactory(club=club, training_type=tt)
        SubscriptionFactory(
            club=club,
            student=child,
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            expires_at=timezone.now() + timedelta(days=3),
        )
        NotificationTemplateFactory(
            club=club,
            trigger_type=NotificationTemplate.TriggerType.SUB_EXPIRY_3D,
            days_before=3,
        )

        from apps.notifications.tasks import check_subscription_expiry

        check_subscription_expiry()
        check_subscription_expiry()

        assert mock_async.call_count == 1
        assert SentNotification.objects.filter(
            club=club,
            student=child,
            notification_type=NotificationTemplate.TriggerType.PARENT_SUB_EXPIRY,
        ).count() == 1


@pytest.mark.django_db
class TestCheckTrainingsLeftPush:
    @patch("apps.notifications.services.async_task")
    def test_trainings_left_2_push(self, mock_async, setup_club_with_student):
        """Checkin that leaves trainings_left=2 triggers push."""
        data = setup_club_with_student
        tt = TrainingTypeFactory(club=data["club"])
        tariff = TariffFactory(club=data["club"], training_type=tt)
        location = LocationFactory(club=data["club"])
        sub = SubscriptionFactory(
            club=data["club"],
            student=data["student"],
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            trainings_left=2,
            expires_at=timezone.now() + timedelta(days=30),
        )
        NotificationTemplateFactory(
            club=data["club"],
            trigger_type=NotificationTemplate.TriggerType.TRAININGS_LEFT_2,
            body_template="{name}, у вас осталось {trainings_left} тренировок",
        )

        from apps.attendance.models import Checkin, Schedule
        from apps.trainers.tests.factories import TrainerFactory

        trainer = TrainerFactory(club=data["club"])
        schedule = Schedule.objects.create(
            club=data["club"],
            day_of_week=0,
            start_time="10:00",
            end_time="11:00",
            group_name="Test",
            trainer=trainer,
            location=location,
        )
        checkin = Checkin.objects.create(
            club=data["club"],
            student=data["student"],
            schedule=schedule,
            training_type=tt,
            trainer=trainer,
            location=location,
            date=timezone.now().date(),
            source="manual",
            subscription=sub,
        )

        from apps.notifications.tasks import check_trainings_left_push

        check_trainings_left_push(checkin.id, data["club"].id)
        assert SentNotification.objects.filter(
            club=data["club"],
            student=data["student"],
            notification_type=NotificationTemplate.TriggerType.TRAININGS_LEFT_2,
        ).exists()

    @patch("apps.notifications.services.async_task")
    def test_trainings_last_push(self, mock_async, setup_club_with_student):
        """Checkin that leaves trainings_left=0 triggers last training push."""
        data = setup_club_with_student
        tt = TrainingTypeFactory(club=data["club"])
        tariff = TariffFactory(club=data["club"], training_type=tt)
        location = LocationFactory(club=data["club"])
        sub = SubscriptionFactory(
            club=data["club"],
            student=data["student"],
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            trainings_left=0,
            expires_at=timezone.now() + timedelta(days=30),
        )
        NotificationTemplateFactory(
            club=data["club"],
            trigger_type=NotificationTemplate.TriggerType.TRAININGS_LAST,
            body_template="{name}, это была последняя тренировка",
        )

        from apps.attendance.models import Checkin, Schedule
        from apps.trainers.tests.factories import TrainerFactory

        trainer = TrainerFactory(club=data["club"])
        schedule = Schedule.objects.create(
            club=data["club"],
            day_of_week=0,
            start_time="10:00",
            end_time="11:00",
            group_name="Test",
            trainer=trainer,
            location=location,
        )
        checkin = Checkin.objects.create(
            club=data["club"],
            student=data["student"],
            schedule=schedule,
            training_type=tt,
            trainer=trainer,
            location=location,
            date=timezone.now().date(),
            source="manual",
            subscription=sub,
        )

        from apps.notifications.tasks import check_trainings_left_push

        check_trainings_left_push(checkin.id, data["club"].id)
        assert SentNotification.objects.filter(
            club=data["club"],
            student=data["student"],
            notification_type=NotificationTemplate.TriggerType.TRAININGS_LAST,
        ).exists()

    @patch("apps.notifications.services.async_task")
    def test_opt_out_no_push(self, mock_async, setup_club_with_student):
        """Student with PushSubscription.is_active=False receives no push."""
        data = setup_club_with_student
        # Deactivate push subscription
        data["push_sub"].is_active = False
        data["push_sub"].save()

        tt = TrainingTypeFactory(club=data["club"])
        tariff = TariffFactory(club=data["club"], training_type=tt)
        location = LocationFactory(club=data["club"])
        sub = SubscriptionFactory(
            club=data["club"],
            student=data["student"],
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            trainings_left=2,
            expires_at=timezone.now() + timedelta(days=30),
        )
        NotificationTemplateFactory(
            club=data["club"],
            trigger_type=NotificationTemplate.TriggerType.TRAININGS_LEFT_2,
            body_template="{name}, осталось {trainings_left}",
        )

        from apps.attendance.models import Checkin, Schedule
        from apps.trainers.tests.factories import TrainerFactory

        trainer = TrainerFactory(club=data["club"])
        schedule = Schedule.objects.create(
            club=data["club"],
            day_of_week=0,
            start_time="10:00",
            end_time="11:00",
            group_name="Test",
            trainer=trainer,
            location=location,
        )
        checkin = Checkin.objects.create(
            club=data["club"],
            student=data["student"],
            schedule=schedule,
            training_type=tt,
            trainer=trainer,
            location=location,
            date=timezone.now().date(),
            source="manual",
            subscription=sub,
        )

        from apps.notifications.tasks import check_trainings_left_push

        check_trainings_left_push(checkin.id, data["club"].id)
        # SentNotification created but no push sent (is_active=False filters out)
        # The send_student_notification still records dedup, but send_push_to_user sends to 0 subs
        assert mock_async.call_count == 0

    @patch("apps.notifications.services.async_task")
    def test_wrong_club_id_does_not_process_checkin(self, mock_async, setup_club_with_student):
        data = setup_club_with_student
        other_club = ClubFactory()
        tt = TrainingTypeFactory(club=data["club"])
        tariff = TariffFactory(club=data["club"], training_type=tt)
        location = LocationFactory(club=data["club"])
        SubscriptionFactory(
            club=data["club"],
            student=data["student"],
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            trainings_left=2,
            expires_at=timezone.now() + timedelta(days=30),
        )
        NotificationTemplateFactory(
            club=data["club"],
            trigger_type=NotificationTemplate.TriggerType.TRAININGS_LEFT_2,
            body_template="{name}, осталось {trainings_left}",
        )

        from apps.attendance.models import Checkin, Schedule
        from apps.trainers.tests.factories import TrainerFactory

        trainer = TrainerFactory(club=data["club"])
        schedule = Schedule.objects.create(
            club=data["club"],
            day_of_week=0,
            start_time="10:00",
            end_time="11:00",
            group_name="Test",
            trainer=trainer,
            location=location,
            training_type=tt,
        )
        checkin = Checkin.objects.create(
            club=data["club"],
            student=data["student"],
            schedule=schedule,
            training_type=tt,
            trainer=trainer,
            location=location,
            date=timezone.now().date(),
            source="manual",
        )

        from apps.notifications.tasks import check_trainings_left_push

        check_trainings_left_push(checkin.id, other_club.id)

        assert not SentNotification.objects.filter(
            club=data["club"],
            student=data["student"],
            notification_type=NotificationTemplate.TriggerType.TRAININGS_LEFT_2,
        ).exists()

    @patch("apps.notifications.services.async_task")
    def test_trainings_left_push_uses_checkin_subscription(self, mock_async, setup_club_with_student):
        data = setup_club_with_student
        tt = TrainingTypeFactory(club=data["club"])
        tariff = TariffFactory(club=data["club"], training_type=tt)
        location = LocationFactory(club=data["club"])
        checkin_sub = SubscriptionFactory(
            club=data["club"],
            student=data["student"],
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            trainings_left=2,
            expires_at=timezone.now() + timedelta(days=30),
        )
        SubscriptionFactory(
            club=data["club"],
            student=data["student"],
            tariff=tariff,
            status=Subscription.Status.ACTIVE,
            trainings_left=5,
            expires_at=timezone.now() + timedelta(days=30),
        )
        NotificationTemplateFactory(
            club=data["club"],
            trigger_type=NotificationTemplate.TriggerType.TRAININGS_LEFT_2,
            body_template="{name}, осталось {trainings_left}",
        )

        from apps.attendance.models import Checkin, Schedule
        from apps.trainers.tests.factories import TrainerFactory

        trainer = TrainerFactory(club=data["club"])
        schedule = Schedule.objects.create(
            club=data["club"],
            day_of_week=0,
            start_time="10:00",
            end_time="11:00",
            group_name="Test",
            trainer=trainer,
            location=location,
            training_type=tt,
        )
        checkin = Checkin.objects.create(
            club=data["club"],
            student=data["student"],
            schedule=schedule,
            training_type=tt,
            trainer=trainer,
            location=location,
            date=timezone.now().date(),
            source="manual",
            subscription=checkin_sub,
        )

        from apps.notifications.tasks import check_trainings_left_push

        check_trainings_left_push(checkin.id, data["club"].id)

        assert SentNotification.objects.filter(
            club=data["club"],
            student=data["student"],
            notification_type=NotificationTemplate.TriggerType.TRAININGS_LEFT_2,
        ).exists()
