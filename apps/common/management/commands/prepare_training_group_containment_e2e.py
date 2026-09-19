from __future__ import annotations

from apps.attendance.models import TrainingGroupRolloutState
from apps.attendance.services.training_groups import transition_training_group_rollout_for_owner
from apps.common.management.commands.prepare_bank_payment_link_e2e import Command as BankPaymentLinkCommand


class Command(BankPaymentLinkCommand):
    help = "Prepare an isolated canonical-group containment fixture for real-stack E2E."

    def _create_fixture(self) -> dict:
        fixture = super()._create_fixture()
        target_group = fixture["target_group"]
        state = transition_training_group_rollout_for_owner(
            club_id=fixture["club_id"],
            target_mode=TrainingGroupRolloutState.Mode.CONTAINMENT,
            actor_id=fixture["trainer"]["user_id"],
            rationale="Isolated real-stack containment compatibility fixture.",
            idempotency_key=f"{fixture['fixture_id']}-containment",
            rollout_gate_digest="",
        )
        target_group["rollout_mode"] = state.mode
        fixture["containment"] = {
            "existing_order_id": fixture["trainer_student"]["order_id"],
            "existing_order_training_group_id": target_group["training_group_id"],
            "new_intent_selection_mode": "disabled",
        }
        fixture["finance_workspace"]["browser_confirms_manual_payment"] = False
        fixture["expected"]["browser_statuses"] = {
            "trainer": "cancelled",
            "student": "pending",
            "parent": "pending",
        }
        return fixture
