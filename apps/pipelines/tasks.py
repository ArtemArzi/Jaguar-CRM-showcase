from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def advance_all_pipelines() -> dict:
    """Periodic task (hourly). Advances all due pipeline executions across all clubs."""
    from apps.clubs.models import Club
    from apps.pipelines.services import advance_due_pipelines

    results = {"clubs": 0, "advanced": 0, "completed": 0}
    for club in Club.objects.filter(is_active=True).only("id"):
        result = advance_due_pipelines(club_id=club.id)
        results["clubs"] += 1
        results["advanced"] += result["advanced"]
        results["completed"] += result["completed"]
    logger.info("advance_all_pipelines_complete", extra=results)
    return results


def seed_default_pipelines_task(club_id: int) -> None:
    """Called from onboarding finish or first lead creation."""
    from apps.pipelines.services import seed_default_pipelines

    seed_default_pipelines(club_id=club_id)
