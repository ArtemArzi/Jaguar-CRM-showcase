from __future__ import annotations

from django.core.management.base import CommandError

from apps.attendance.models import TrainingGroupRolloutState
from apps.billing.models import BankPaymentOrder
from apps.common.management.commands.assert_bank_payment_link_e2e import Command as BankPaymentLinkAssertCommand


class Command(BankPaymentLinkAssertCommand):
    help = "Assert containment preserves existing canonical bank-order lifecycle evidence."

    def _load_fixture(self, path):
        fixture = super()._load_fixture(path)
        containment = fixture.get("containment")
        if (
            fixture["target_group"].get("rollout_mode") != TrainingGroupRolloutState.Mode.CONTAINMENT
            or not isinstance(containment, dict)
            or not isinstance(containment.get("existing_order_id"), int)
        ):
            raise CommandError("fixture must describe an existing canonical order in containment")
        return fixture

    def _collect_evidence(self, fixture: dict) -> dict:
        evidence = super()._collect_evidence(fixture)
        club_id = int(fixture["club_id"])
        state = TrainingGroupRolloutState.objects.for_club(club_id).get()
        if state.mode != TrainingGroupRolloutState.Mode.CONTAINMENT:
            raise CommandError("containment fixture rollout state changed unexpectedly")
        order = BankPaymentOrder.objects.for_club(club_id).select_related("payment").get(
            id=int(fixture["containment"]["existing_order_id"])
        )
        if order.payment.target_training_group_id != int(
            fixture["containment"]["existing_order_training_group_id"]
        ):
            raise CommandError("existing containment order lost canonical group identity")
        evidence["containment"] = {
            "mode": state.mode,
            "existing_order_id": order.id,
            "existing_order_training_group_id": order.payment.target_training_group_id,
            "new_intent_selection_mode": fixture["containment"]["new_intent_selection_mode"],
        }
        return evidence
