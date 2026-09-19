from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from apps.attendance.models import PersonalDropInPaymentLink
from apps.billing.models import Payment
from apps.clubs.models import Club
from apps.leads.services import admit_lead_for_manual_operational_admission
from apps.students.operational_admission_contracts import ManualOperationalAdmissionEvidence


def _origin_for_reconciliation(*, payment: Payment) -> str:
    """Identify a candidate origin without treating it as sufficient evidence."""

    try:
        payment.personal_drop_in_payment_link
    except PersonalDropInPaymentLink.DoesNotExist:
        pass
    else:
        return "personal"

    if (
        payment.target_training_group_id is not None
        and payment.group_membership_action_snapshot
        == Payment.GroupMembershipActionSnapshot.NEW_ADMISSION
    ):
        return "group"
    raise CommandError("payment has no complete candidate origin")


class Command(BaseCommand):
    help = "Reconcile one exact pending manual operational admission for one explicit club."

    def add_arguments(self, parser):
        parser.add_argument("--club-id", type=int, required=True)
        parser.add_argument("--payment-id", type=int, required=True)
        parser.add_argument("--actor-user-id", type=int, required=True)
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Execute the evidence-bound idempotent reconciliation; omitted is read-only.",
        )

    def handle(self, *args, **options):
        club = Club.objects.filter(id=options["club_id"]).first()
        if club is None:
            raise CommandError("club not found")
        payment = (
            Payment.objects.for_club(club)
            .select_related(
                "student",
                "subscription",
                "conversion_group_membership",
                "conversion_enrollment",
                "personal_drop_in_payment_link__booking__enrollment",
            )
            .filter(id=options["payment_id"])
            .first()
        )
        if payment is None:
            raise CommandError("payment not found")
        origin = _origin_for_reconciliation(payment=payment)
        # This command only advances complete exact rows classified by the
        # read-only audit. A canonical row is accepted solely as an idempotent
        # replay: the evidence owner will validate the immutable family again
        # and return ``already_admitted`` without touching the person.
        from apps.billing.management.commands.audit_operational_admissions import (
            _classify_pending_manual_row,
            _exact_personal_payment_link,
        )

        classification = _classify_pending_manual_row(
            payment=payment,
            personal_link=_exact_personal_payment_link(payment=payment),
        )
        if classification not in {"reconcile", "canonical"}:
            raise CommandError("payment is not an exact reconciliation candidate")
        receipt = {
            "club_id": club.id,
            "payment_id": payment.id,
            "origin": origin,
        }
        if not options["apply"]:
            receipt["outcome"] = "dry_run"
            self.stdout.write(json.dumps(receipt, sort_keys=True))
            return

        admitted = admit_lead_for_manual_operational_admission(
            evidence=ManualOperationalAdmissionEvidence(
                club_id=club.id,
                student_id=payment.student_id,
                payment_id=payment.id,
                origin=origin,
                actor_user_id=options["actor_user_id"],
            ),
            occurred_at=payment.created_at if classification == "reconcile" else None,
        )
        receipt["outcome"] = "admitted" if admitted else "already_admitted"
        self.stdout.write(json.dumps(receipt, sort_keys=True))
