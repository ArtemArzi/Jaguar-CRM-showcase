from __future__ import annotations

from dataclasses import fields

import pytest
from django.db import DatabaseError
from pydantic import ValidationError

from apps.attendance.models import PersonalBookingPaymentReservation, PersonalDropInBooking
from apps.attendance.schemas import (
    PersonalAvailabilityDirectStaffIntentV2In,
    PersonalAvailabilityStaffIntentV2In,
)
from apps.billing.models import BankPaymentOrder, Payment, Subscription
from apps.billing.schemas import GroupSaleBankOrderV2In, GroupSaleManualV2In
from apps.clubs.capabilities import (
    GROUP_SALE_BANK_ORDER_V2_ROUTE,
    GROUP_SALE_MANUAL_V2_ROUTE,
    PERSONAL_STAFF_DIRECT_INTENT_V2_ROUTE,
    PERSONAL_STAFF_SLOT_INTENT_V2_ROUTE,
    assert_v2_manual_admission_command_allowed,
    get_commercial_journey_capability,
    get_v1_commercial_journey_command_availability,
    get_v2_group_manual_admission_command_availability,
    get_v2_group_provider_command_availability,
    get_v2_manual_admission_command_availability,
    get_v2_provider_command_availability,
)
from apps.clubs.models import ClubSettings
from apps.clubs.schemas import CommercialJourneyCommandResultOut
from apps.clubs.tests.factories import ClubSettingsFactory
from apps.common.exceptions import BusinessLogicError
from apps.students.operational_admission_contracts import ManualOperationalAdmissionEvidence


@pytest.mark.django_db
class TestCommercialJourneyCapability:
    def test_defaults_to_durable_v1_with_v2_writes_disabled(self, club, settings):
        ClubSettingsFactory(club=club, unified_client_journey_enabled=True)
        settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
        settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED = True

        capability = get_commercial_journey_capability(club=club)

        assert capability.protocol_version == ClubSettings.CommercialJourneyProtocol.V1
        assert capability.v2_commercial_journey_enabled is False
        assert capability.v2_manual_admission_enabled is False

    @pytest.mark.parametrize(
        ("global_unified", "tenant_unified", "manual_gate", "protocol", "expected"),
        [
            (False, True, True, ClubSettings.CommercialJourneyProtocol.V2, False),
            (True, False, True, ClubSettings.CommercialJourneyProtocol.V2, False),
            (True, True, False, ClubSettings.CommercialJourneyProtocol.V2, False),
            (True, True, True, ClubSettings.CommercialJourneyProtocol.V1, False),
            (True, True, True, ClubSettings.CommercialJourneyProtocol.V2, True),
        ],
    )
    def test_v2_manual_admission_fails_closed_until_every_required_gate_is_enabled(
        self,
        club,
        settings,
        global_unified,
        tenant_unified,
        manual_gate,
        protocol,
        expected,
    ):
        ClubSettingsFactory(
            club=club,
            unified_client_journey_enabled=tenant_unified,
            commercial_journey_protocol_version=protocol,
        )
        settings.UNIFIED_CLIENT_JOURNEY_ENABLED = global_unified
        settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED = manual_gate

        capability = get_commercial_journey_capability(club=club)

        assert capability.v2_manual_admission_enabled is expected

    def test_database_error_fails_closed_without_assuming_a_protocol(self, club, monkeypatch):
        def raise_database_error(*args, **kwargs):
            raise DatabaseError("schema unavailable")

        monkeypatch.setattr(
            "apps.clubs.capabilities.ClubSettings.objects.filter",
            raise_database_error,
        )

        capability = get_commercial_journey_capability(club=club)

        assert capability.protocol_version == "invalid"
        assert capability.unified_client_journey_enabled is False
        assert capability.v2_commercial_journey_enabled is False
        assert capability.v2_manual_admission_enabled is False

    def test_v2_tenant_rejects_new_v1_command_with_typed_upgrade_result(self, club, settings):
        ClubSettingsFactory(
            club=club,
            unified_client_journey_enabled=True,
            commercial_journey_protocol_version=ClubSettings.CommercialJourneyProtocol.V2,
        )
        settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
        settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED = True

        availability = get_v1_commercial_journey_command_availability(
            capability=get_commercial_journey_capability(club=club),
        )

        assert availability.allows_new_command is False
        assert availability.code == "client_upgrade_required"
        assert not PersonalDropInBooking.objects.for_club(club).exists()
        assert not PersonalBookingPaymentReservation.objects.for_club(club).exists()
        assert not Payment.objects.for_club(club).exists()
        assert not Subscription.objects.for_club(club).exists()
        assert not BankPaymentOrder.objects.for_club(club).exists()

    def test_v1_command_fails_closed_when_its_existing_unified_gate_is_off(self, club, settings):
        ClubSettingsFactory(club=club, unified_client_journey_enabled=True)
        settings.UNIFIED_CLIENT_JOURNEY_ENABLED = False

        availability = get_v1_commercial_journey_command_availability(
            capability=get_commercial_journey_capability(club=club),
        )

        assert availability.allows_new_command is False
        assert availability.code == "commercial_journey_unavailable"

    @pytest.mark.parametrize(
        ("protocol", "manual_gate", "expected_code"),
        [
            (ClubSettings.CommercialJourneyProtocol.V2, False, "commercial_journey_unavailable"),
        ],
    )
    def test_prewrite_guard_returns_typed_denial_before_any_commercial_artifact_exists(
        self,
        club,
        settings,
        protocol,
        manual_gate,
        expected_code,
    ):
        ClubSettingsFactory(
            club=club,
            unified_client_journey_enabled=True,
            commercial_journey_protocol_version=protocol,
        )
        settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
        settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED = manual_gate

        with pytest.raises(BusinessLogicError) as exc_info:
            assert_v2_manual_admission_command_allowed(
                capability=get_commercial_journey_capability(club=club),
            )

        assert exc_info.value.code == expected_code
        assert not PersonalDropInBooking.objects.for_club(club).exists()
        assert not PersonalBookingPaymentReservation.objects.for_club(club).exists()
        assert not Payment.objects.for_club(club).exists()
        assert not Subscription.objects.for_club(club).exists()
        assert not BankPaymentOrder.objects.for_club(club).exists()

    def test_accepted_replay_or_drain_remains_available_when_every_new_write_gate_is_off(
        self,
        club,
        settings,
    ):
        ClubSettingsFactory(
            club=club,
            unified_client_journey_enabled=False,
            commercial_journey_protocol_version=ClubSettings.CommercialJourneyProtocol.V2,
        )
        settings.UNIFIED_CLIENT_JOURNEY_ENABLED = False
        settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED = False

        availability = get_v2_manual_admission_command_availability(
            capability=get_commercial_journey_capability(club=club),
            accepted_replay_or_drain=True,
        )

        assert availability.allows_new_command is True
        assert availability.code == "replay_or_drain"

    @pytest.mark.parametrize(
        ("rollout_mode", "group_new_writes", "expected"),
        [
            ("missing", True, False),
            ("off", True, False),
            ("reconciling", True, False),
            ("shadow", True, False),
            ("active", False, False),
            ("containment", True, False),
            ("active", True, True),
        ],
    )
    def test_v2_group_sale_requires_active_rollout_and_global_group_write_gate(
        self,
        club,
        settings,
        rollout_mode,
        group_new_writes,
        expected,
    ):
        ClubSettingsFactory(
            club=club,
            unified_client_journey_enabled=True,
            commercial_journey_protocol_version=ClubSettings.CommercialJourneyProtocol.V2,
        )
        settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
        settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED = True

        availability = get_v2_group_manual_admission_command_availability(
            capability=get_commercial_journey_capability(club=club),
            training_group_rollout_mode=rollout_mode,
            training_group_new_writes_enabled=group_new_writes,
        )

        assert availability.allows_new_command is expected
        assert availability.code == ("available" if expected else "commercial_journey_unavailable")

    def test_v2_group_manual_admission_stays_unavailable_when_its_manual_gate_is_off(
        self,
        club,
        settings,
    ):
        ClubSettingsFactory(
            club=club,
            unified_client_journey_enabled=True,
            commercial_journey_protocol_version=ClubSettings.CommercialJourneyProtocol.V2,
        )
        settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
        settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED = False

        availability = get_v2_group_manual_admission_command_availability(
            capability=get_commercial_journey_capability(club=club),
            training_group_rollout_mode="active",
            training_group_new_writes_enabled=True,
        )

        assert availability.allows_new_command is False
        assert availability.code == "commercial_journey_unavailable"
        assert not Payment.objects.for_club(club).exists()
        assert not Subscription.objects.for_club(club).exists()
        assert not BankPaymentOrder.objects.for_club(club).exists()

    @pytest.mark.parametrize(
        ("provider_capable", "expected"),
        [(False, False), (True, True)],
    )
    def test_v2_sbp_is_independent_of_manual_admission_gate_and_requires_provider_readiness(
        self,
        club,
        settings,
        provider_capable,
        expected,
    ):
        ClubSettingsFactory(
            club=club,
            unified_client_journey_enabled=True,
            commercial_journey_protocol_version=ClubSettings.CommercialJourneyProtocol.V2,
        )
        settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
        settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED = False
        capability = get_commercial_journey_capability(club=club)

        manual = get_v2_manual_admission_command_availability(capability=capability)
        provider = get_v2_provider_command_availability(
            capability=capability,
            provider_creation_enabled=provider_capable,
        )

        assert manual.allows_new_command is False
        assert provider.allows_new_command is expected
        assert provider.code == ("available" if expected else "commercial_journey_unavailable")
        assert not PersonalDropInBooking.objects.for_club(club).exists()
        assert not PersonalBookingPaymentReservation.objects.for_club(club).exists()
        assert not Payment.objects.for_club(club).exists()
        assert not Subscription.objects.for_club(club).exists()
        assert not BankPaymentOrder.objects.for_club(club).exists()

    @pytest.mark.parametrize(
        ("rollout_mode", "group_new_writes", "expected"),
        [
            ("missing", True, False),
            ("off", True, False),
            ("reconciling", True, False),
            ("shadow", True, False),
            ("active", False, False),
            ("containment", True, False),
            ("active", True, True),
        ],
    )
    def test_v2_group_sbp_requires_provider_and_active_canonical_group_gates_but_not_manual_gate(
        self,
        club,
        settings,
        rollout_mode,
        group_new_writes,
        expected,
    ):
        ClubSettingsFactory(
            club=club,
            unified_client_journey_enabled=True,
            commercial_journey_protocol_version=ClubSettings.CommercialJourneyProtocol.V2,
        )
        settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
        settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED = False

        availability = get_v2_group_provider_command_availability(
            capability=get_commercial_journey_capability(club=club),
            provider_creation_enabled=True,
            training_group_rollout_mode=rollout_mode,
            training_group_new_writes_enabled=group_new_writes,
        )

        assert availability.allows_new_command is expected
        assert availability.code == ("available" if expected else "commercial_journey_unavailable")

    def test_v2_group_sbp_fails_closed_when_the_provider_cannot_create_a_link(
        self,
        club,
        settings,
    ):
        ClubSettingsFactory(
            club=club,
            unified_client_journey_enabled=True,
            commercial_journey_protocol_version=ClubSettings.CommercialJourneyProtocol.V2,
        )
        settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
        settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED = False

        availability = get_v2_group_provider_command_availability(
            capability=get_commercial_journey_capability(club=club),
            provider_creation_enabled=False,
            training_group_rollout_mode="active",
            training_group_new_writes_enabled=True,
        )

        assert availability.allows_new_command is False
        assert availability.code == "commercial_journey_unavailable"
        assert not Payment.objects.for_club(club).exists()
        assert not Subscription.objects.for_club(club).exists()
        assert not BankPaymentOrder.objects.for_club(club).exists()


def test_v2_route_markers_are_distinct_from_the_legacy_routes():
    assert PERSONAL_STAFF_SLOT_INTENT_V2_ROUTE == "/personal-availability/v2/slots/{slot_id}/staff-intents/"
    assert PERSONAL_STAFF_DIRECT_INTENT_V2_ROUTE == "/personal-availability/v2/staff-intents/direct/"
    assert GROUP_SALE_MANUAL_V2_ROUTE == "/billing/v2/group-sales/manual/"
    assert GROUP_SALE_BANK_ORDER_V2_ROUTE == "/billing/v2/group-sales/bank-orders/"


def test_v2_command_schemas_require_a_version_marker_and_forbid_unowned_fields():
    personal = PersonalAvailabilityStaffIntentV2In.model_validate(
        {
            "protocol_version": "v2",
            "student_id": 10,
            "payment_method": "cash",
            "offer_digest": "digest",
            "idempotency_key": "personal-k1",
        }
    )
    direct = PersonalAvailabilityDirectStaffIntentV2In.model_validate(
        {
            **personal.model_dump(),
            "trainer_id": 4,
            "starts_at": "2026-09-10T10:00:00Z",
            "ends_at": "2026-09-10T11:00:00Z",
            "location_id": 3,
            "training_type_id": 2,
        }
    )
    group_manual = GroupSaleManualV2In.model_validate(
        {
            "protocol_version": "v2",
            "student_id": 10,
            "tariff_id": 9,
            "payment_method": "transfer",
            "target_training_group_id": 7,
            "target_schedule_id": 8,
            "target_start_date": "2026-09-10",
            "expected_offer_digest": "digest",
            "idempotency_key": "group-k1",
        }
    )
    group_sbp = GroupSaleBankOrderV2In.model_validate(
        {
            **group_manual.model_dump(exclude={"payment_method"}),
            "buyer_email": "buyer@example.test",
        }
    )

    assert direct.protocol_version == group_manual.protocol_version == group_sbp.protocol_version == "v2"
    with pytest.raises(ValidationError):
        PersonalAvailabilityStaffIntentV2In.model_validate(
            {**personal.model_dump(), "amount": "1.00"}
        )
    with pytest.raises(ValidationError):
        GroupSaleManualV2In.model_validate(
            {key: value for key, value in group_manual.model_dump().items() if key != "protocol_version"}
        )


def test_versioned_result_vocabulary_keeps_replay_orthogonal_to_workspace_and_finance_state():
    result = CommercialJourneyCommandResultOut.model_validate(
        {
            "workspace_state": "student",
            "finance_state": "pending_manual",
            "command_replayed": True,
        }
    )

    assert result.workspace_state == "student"
    assert result.finance_state == "pending_manual"
    assert result.command_replayed is True
    with pytest.raises(ValidationError):
        CommercialJourneyCommandResultOut.model_validate(
            {
                "workspace_state": "student",
                "finance_state": "replayed",
                "command_replayed": False,
            }
        )


def test_evidence_owner_contract_requires_explicit_tenant_student_payment_origin_and_actor():
    assert [field.name for field in fields(ManualOperationalAdmissionEvidence)] == [
        "club_id",
        "student_id",
        "payment_id",
        "origin",
        "actor_user_id",
    ]
