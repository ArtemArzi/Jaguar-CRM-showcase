import json
from decimal import Decimal
from time import monotonic, sleep

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Sum

from apps.attendance.models import Checkin, GroupSession, StudentAttendanceCorrection
from apps.billing.models import Payment, Subscription, SubscriptionComponent, SubscriptionCorrection
from apps.common.management.commands.prepare_student_operations_e2e import load_owned_fixture
from apps.trainers.models import TrainerEarning


class Command(BaseCommand):
    help = "Assert student operations persisted state without exposing fixture identities or credentials."

    def add_arguments(self, parser):
        parser.add_argument("--fixture", required=True)
        parser.add_argument("--phase", choices=["corrected", "final"], required=True)

    def handle(self, *args, **options):
        fixture = load_owned_fixture(options["fixture"])
        phase = options["phase"]
        deadline = monotonic() + 30
        evidence = {}
        while monotonic() < deadline:
            evidence = self.evidence(fixture, phase)
            if all(evidence.values()):
                self.stdout.write(json.dumps({"ok": True, "checks": evidence}))
                return
            sleep(0.2)
        raise CommandError(json.dumps({"ok": False, "checks": evidence}))

    def evidence(self, fixture, phase):
        club_id = fixture["club_id"]
        sub = Subscription.objects.for_club(club_id).get(id=fixture["subscription_id"])
        component = SubscriptionComponent.objects.for_club(club_id).get(id=fixture["component_id"])
        checkins = Checkin.objects.for_club(club_id).filter(student_id=fixture["student"]["id"])
        corrections = SubscriptionCorrection.objects.for_club(club_id).filter(subscription=sub)
        evidence = {
            "money_preserved": component.unit_amount_basis_snapshot == Decimal("1000")
            and component.paid_amount_basis_snapshot == Decimal("8000"),
            "control_untouched": not Checkin.objects.for_club(club_id)
            .filter(
                student_id=fixture["control_student_id"],
            )
            .exists(),
            "no_session_closed": not GroupSession.objects.for_club(club_id).filter(closed_at__isnull=False).exists(),
        }
        if phase == "corrected":
            evidence.update(
                counters=(component.credits_left, component.credits_used) == (9, 1),
                aggregate=(sub.trainings_left, sub.trainings_used) == (9, 1),
                correction=corrections.count() == 1,
                no_attendance=not checkins.exists(),
            )
        else:
            active = checkins.filter(cancelled_at__isnull=True, deleted_at__isnull=True)
            earnings = TrainerEarning.objects.for_club(club_id).filter(
                checkin__student_id=fixture["student"]["id"],
                cancelled=False,
            ).aggregate(total=Sum("amount"))["total"] or Decimal("0")
            renewals = Payment.objects.for_club(club_id).filter(subscription__renewed_from=sub)
            evidence.update(
                counters=(component.credits_left, component.credits_used) == (10, 2),
                aggregate=(sub.trainings_left, sub.trainings_used) == (10, 2),
                corrections=corrections.count() == 2,
                attendance=checkins.count() == 2 and active.count() == 1,
                cancelled=checkins.filter(cancelled_at__isnull=False).count() == 1,
                receipts=StudentAttendanceCorrection.objects.for_club(club_id).count() == 3,
                salary=earnings == Decimal("500"),
                exact_renewal=renewals.count() == 1
                and renewals.filter(
                    status="pending",
                    subscription__status="pending",
                ).exists(),
            )
        return evidence
