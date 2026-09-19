"""Read-only reconciliation against purchased terms, exact carry and corrections."""

from apps.billing.models import SubscriptionCorrection, SubscriptionRenewalEvent, TariffComponent
from apps.billing.service_modules.entitlements import component_aggregate_counters
from apps.common.exceptions import BusinessLogicError


def component_incoming_carry(*, subscription, component):
    events = list(SubscriptionRenewalEvent.objects.for_club(subscription.club_id).filter(renewed_to=subscription))
    if subscription.renewed_from_id and not events:
        raise BusinessLogicError("Нет подтверждения переноса остатка.", code="correction_carry_needs_review")
    carry = 0
    for event in events:
        snapshot = event.carry_snapshot
        if snapshot.get("legacy_finite_credits", 0):
            # Old scalar carry did not establish a component destination. Do not
            # manufacture that historical attribution from today's tariff.
            raise BusinessLogicError("Нужно сверить перенос старого остатка.", code="correction_carry_needs_review")
        for row in snapshot.get("components", []):
            if row.get("to_component_id") != component.id:
                continue
            value = row.get("credits")
            component_source = event.renewed_from.components.filter(
                id=row.get("from_component_id"), club_id=subscription.club_id,
            ).exists()
            legacy_source = (
                row.get("from_legacy_subscription_id") == event.renewed_from_id
                and row.get("source_training_type_id") == component.training_type_id
                and row.get("source_scope") == component.scope
                and "source_location_id" in row and row["source_location_id"] == component.location_id
                and not event.renewed_from.components.filter(club_id=subscription.club_id).exists()
            )
            if (
                row.get("kind") != "finite_credits"
                or type(value) is not int
                or value < 0
                or not (component_source or legacy_source)
            ):
                raise BusinessLogicError("Нужно сверить доказательство переноса.", code="correction_carry_needs_review")
            carry += value
    return carry


def subscription_balance_findings(*, subscription, components=None):
    components = (
        list(subscription.components.filter(club_id=subscription.club_id).order_by("id"))
        if components is None
        else components
    )
    findings = []
    for component in components:
        if component.entitlement_kind != TariffComponent.EntitlementKind.FINITE_CREDITS:
            continue
        try:
            carry = component_incoming_carry(subscription=subscription, component=component)
        except BusinessLogicError as error:
            findings.append({"code": error.code, "component_id": component.id})
            continue
        delta = sum(
            SubscriptionCorrection.objects.for_club(subscription.club_id)
            .filter(
                component=component,
                subscription=subscription,
            )
            .values_list("balance_delta", flat=True)
        )
        expected = component.credits_total + carry - component.credits_used + delta
        if expected != component.credits_left:
            findings.append(
                {
                    "code": "component_balance_mismatch",
                    "component_id": component.id,
                    "expected": expected,
                    "actual": component.credits_left,
                }
            )
    if components:
        left, used = component_aggregate_counters(components=components)
        if (left, used) != (subscription.trainings_left, subscription.trainings_used):
            findings.append(
                {
                    "code": "aggregate_balance_mismatch",
                    "expected_left": left,
                    "expected_used": used,
                    "actual_left": subscription.trainings_left,
                    "actual_used": subscription.trainings_used,
                }
            )
    return findings
