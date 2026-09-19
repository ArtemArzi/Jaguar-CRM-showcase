from apps.attendance.models import _TRAINING_GROUP_ROLLOUT_TRANSITION_TOKEN


def update_training_group_rollout_state_for_test(queryset, **changes) -> int:
    """Bypass transition prerequisites only when arranging an isolated test."""
    return queryset._update_for_transition(
        transition_token=_TRAINING_GROUP_ROLLOUT_TRANSITION_TOKEN,
        **changes,
    )
