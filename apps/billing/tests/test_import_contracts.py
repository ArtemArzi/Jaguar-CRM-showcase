from __future__ import annotations

import ast
import inspect
import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

import apps.billing.services as billing_services
from apps.billing.service_modules import (
    bank_order_review,
    bank_orders,
    catalog,
    club_settings,
    debts,
    entitlements,
    expenses,
    freezes,
    group_payments,
    payment_creation,
    payment_review,
    provider_events,
    renewals,
    sale_earnings,
    subscriptions,
    tariff_components,
)

ROOT = Path(__file__).resolve().parents[3]
ALLOWLIST_PATH = Path(__file__).with_name("import_contract_allowlist.json")
PUBLIC_CONTRACT_PATH = Path(__file__).with_name("public_interface_contract.json")
SERVICE_MODULE = ".".join(("apps", "billing", "services"))
SERVICE_PACKAGE = ".".join(("apps", "billing"))
CURRENT_MIGRATION_SLICE = 13

PUBLIC_INTERFACE = {
    "correct_subscription",
    "preview_subscription_correction",
    "approve_freeze",
    "cancel_bank_payment_order",
    "create_bank_payment_order",
    "create_v2_group_sale_bank_order",
    "create_v2_group_sale_manual",
    "create_discount",
    "create_expense",
    "create_payment",
    "create_subscription",
    "create_tariff",
    "create_training_type",
    "delete_expense",
    "expire_bank_payment_orders",
    "freeze_subscription",
    "get_or_create_club_settings",
    "is_training_type_kind_locked",
    "process_bank_payment_webhook",
    "reject_freeze",
    "replay_deferred_bank_payment_provider_events",
    "resolve_bank_payment_order_manual_review",
    "resolve_v2_group_sale_offer",
    "unfreeze_subscription",
    "update_club_settings",
    "update_discount",
    "update_expense",
    "update_tariff",
    "update_training_type",
    "verify_payment",
    "write_off_debt",
}

PRIVATE_MIGRATION_SLICE = {
    "__all__": 13,
    "_assert_payment_reservation_capacity": 4,
    "_apply_discounts": 7,
    "_debt_state": 4,
    "_enqueue_sale_earning_after_commit": 6,
    "_record_debt_lifecycle_event": 4,
    "_record_debt_settlement_events": 4,
    "_student_has_current_component_subscription": 3,
    "_validate_group_conversion_target": 5,
    "_validate_trainer_group_payment_contract": 5,
    "logger.info": 7,
    "services": 5,
    "timezone": 8,
}

MOVED_PRIVATE_IMPLEMENTATIONS = {
    1: {"_apply_updates", "_money", "_validate_positive_money"},
    2: {
        "_active_tariff_components",
        "_component_from_tariff_defaults",
        "_default_payout_policy_for_kind",
        "_ensure_tariff_components",
        "_has_open_personal_drop_in_for_contract_change",
        "_payment_preflight_tariff_components",
        "_replace_tariff_components",
        "_resolve_grade_system_id_for_training_type",
        "_resolve_tariff_payout_policy",
        "_tariff_component_contracts_equal",
        "_tariff_has_default_component",
        "_validate_component_payloads",
        "_validate_discount_value",
        "_validate_payout_policy",
        "_validate_training_type_kind",
    },
    3: {
        "_allocate_component_paid_amounts",
        "_component_unit_basis",
        "_create_subscription_components",
        "_deduct_subscription_component_for_checkin",
        "_find_subscription_component_for_checkin",
        "_requires_package_owner_for_components",
        "_resolve_package_owner_trainer_id_for_components",
        "_resolve_package_owner_trainer_id_for_sale",
        "_student_has_current_component_subscription",
        "_student_has_current_subscription",
        "_subscription_component_has_weekly_capacity",
    },
    4: {
        "_assert_payment_reservation_capacity",
        "_assert_personal_drop_in_payment_path",
        "_attach_debts_to_subscription",
        "_attach_subscription_to_pending_payment_debts",
        "_closed_period_personal_drop_in_debt_ids",
        "_debt_state",
        "_personal_drop_in_bookings_by_debt",
        "_record_debt_lifecycle_event",
        "_record_debt_settlement_events",
        "_reserve_debts_for_payment",
        "_validate_personal_drop_in_debt_tariff_contract",
        "write_off_debt",
    },
    5: {
        "_close_payment_owned_group_membership",
        "_enroll_paid_conversion_target",
        "_link_payment_owned_group_membership",
        "_lock_and_validate_payment_conversion_enrollment_for_confirm",
        "_resolve_canonical_group_payment_target",
        "_snapshot_group_conversion_target",
        "_trainer_snapshot_name",
        "_validate_group_conversion_target",
        "_validate_payment_conversion_target_for_confirm",
        "_validate_trainer_group_payment_contract",
    },
    6: {
        "_capture_sale_earning_snapshot",
        "_capture_sale_earning_snapshots_for_components",
        "_enqueue_sale_earning_after_commit",
        "_sale_trainer_id_for_component",
        "_validate_direct_subscription_payment_method",
        "create_subscription",
    },
    7: {
        "_apply_discounts",
        "_find_existing_manual_operational_admission",
        "_validate_payment_method",
        "create_payment",
    },
    8: {"verify_payment"},
    9: {
        "get_or_create_club_settings",
        "update_club_settings",
    },
    10: {
        "_get_freeze_for_decision",
        "_mark_expired_subscription_if_needed",
        "_subscription_status_after_unfreeze",
        "_validate_freeze_reason",
        "_validate_positive_days",
        "_validate_subscription_freeze_policy",
        "approve_freeze",
        "freeze_subscription",
        "reject_freeze",
        "unfreeze_subscription",
    },
    11: {
        "_active_renewed_from_subscription",
        "_assert_self_service_bank_order_allowed",
        "_cancel_pending_bank_order_artifacts",
        "_find_reusable_bank_payment_order",
        "_fiscal_item_snapshot",
        "_generate_provider_payment_link_id",
        "_payment_order_purpose",
        "_payment_order_ttl_minutes",
        "_receipt_mode",
        "_receipt_status_for_mode",
        "_self_service_bank_order_source",
        "cancel_bank_payment_order",
        "create_bank_payment_order",
        "expire_bank_payment_orders",
    },
    12: {
        "_clean_review_reason",
        "_ensure_refund_case_for_provider_event",
        "_resolve_bank_order_for_webhook",
        "_mark_order_manual_review",
        "_provider_status_to_order_status",
        "_refund_case_for_manual_review",
        "_replay_deferred_bank_payment_provider_event",
        "_replay_duplicate_provider_event_if_deferred",
        "_sanitize_review_evidence",
        "_should_confirm_expired_order",
        "_webhook_matches_order",
        "_webhook_request_body_bytes",
        "process_bank_payment_webhook",
        "replay_deferred_bank_payment_provider_events",
        "resolve_bank_payment_order_manual_review",
    },
    14: {
        "_carry_compatible_components",
        "_component_identity",
        "_grandfather_legacy_renewal_chain",
        "_lock_renewal_subscriptions",
        "_locked_subscription_components",
        "_replay_payment_command",
        "_require_command_identity",
        "_subscription_has_usable_entitlement",
        "build_subscription_command_fingerprint",
        "create_manual_subscription_renewal",
        "finalize_subscription_renewal",
    },
}


def _tracked_paths() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [ROOT / value for value in result.stdout.splitlines() if value]


def _string_target(value: str) -> str:
    suffix = value.split(SERVICE_MODULE, maxsplit=1)[1].lstrip(".")
    return suffix or "services"


def _python_occurrences(path: Path) -> Counter[tuple[str, str]]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    module_aliases: set[str] = set()
    import_module_aliases: set[str] = set()
    occurrences: Counter[tuple[str, str]] = Counter()

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == SERVICE_MODULE:
            for alias in node.names:
                occurrences[("import-from", alias.name)] += 1
        elif isinstance(node, ast.ImportFrom) and node.module == SERVICE_PACKAGE:
            for alias in node.names:
                if alias.name == "services":
                    module_aliases.add(alias.asname or alias.name)
                    occurrences[("module-import", "services")] += 1
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == SERVICE_MODULE:
                    module_aliases.add(alias.asname or alias.name.rsplit(".", maxsplit=1)[-1])
                    occurrences[("module-import", "services")] += 1
                elif alias.name == "importlib":
                    import_module_aliases.add(f"{alias.asname or alias.name}.import_module")
        elif isinstance(node, ast.ImportFrom) and node.module == "importlib":
            for alias in node.names:
                if alias.name == "import_module":
                    import_module_aliases.add(alias.asname or alias.name)

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in module_aliases
        ):
            occurrences[("module-attribute", node.attr)] += 1
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if SERVICE_MODULE in node.value:
                occurrences[("string-target", _string_target(node.value))] += 1
        if not isinstance(node, ast.Call):
            continue

        call_name = ""
        if isinstance(node.func, ast.Name):
            call_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Name):
                call_name = f"{node.func.value.id}.{node.func.attr}"
            else:
                call_name = node.func.attr

        if (
            call_name in {"getattr", "setattr", "delattr"}
            or call_name.endswith((".object", ".setattr", ".delattr"))
        ) and len(node.args) >= 2:
            target, symbol = node.args[:2]
            if (
                isinstance(target, ast.Name)
                and target.id in module_aliases
                and isinstance(symbol, ast.Constant)
                and isinstance(symbol.value, str)
            ):
                occurrences[("dynamic-attribute", symbol.value)] += 1

        if call_name in import_module_aliases and node.args:
            module_name = node.args[0]
            if (
                isinstance(module_name, ast.Constant)
                and module_name.value == SERVICE_MODULE
            ):
                occurrences[("dynamic-import", "services")] += 1

    return occurrences


def _text_occurrences(path: Path) -> Counter[tuple[str, str]]:
    occurrences: Counter[tuple[str, str]] = Counter()
    for line in path.read_text(encoding="utf-8").splitlines():
        if SERVICE_MODULE in line:
            occurrences[("text-target", _string_target(line))] += 1
    return occurrences


def _migration_slice(symbol: str) -> int:
    if symbol in PUBLIC_INTERFACE:
        return 13
    if symbol in PRIVATE_MIGRATION_SLICE:
        return PRIVATE_MIGRATION_SLICE[symbol]
    raise AssertionError(f"Unclassified billing services symbol: {symbol}")


def build_occurrence_manifest() -> list[dict[str, int | str]]:
    manifest: list[dict[str, int | str]] = []
    for path in _tracked_paths():
        if path in {ALLOWLIST_PATH, PUBLIC_CONTRACT_PATH}:
            continue
        relative = path.relative_to(ROOT).as_posix()
        if path.suffix == ".py":
            occurrences = _python_occurrences(path)
        elif (
            relative.startswith(("apps/", "tests/", "scripts/", "config/"))
            and path.suffix in {".json", ".sh", ".toml", ".txt", ".yaml", ".yml"}
        ):
            try:
                occurrences = _text_occurrences(path)
            except UnicodeDecodeError:
                continue
        else:
            continue

        for (kind, symbol), count in occurrences.items():
            manifest.append(
                {
                    "path": relative,
                    "kind": kind,
                    "symbol": symbol,
                    "migration_slice": _migration_slice(symbol),
                    "count": count,
                }
            )
    return sorted(
        manifest,
        key=lambda row: (
            str(row["path"]),
            str(row["kind"]),
            str(row["symbol"]),
        ),
    )


def build_public_interface_contract() -> dict[str, dict[str, str]]:
    contract = {}
    for name in sorted(PUBLIC_INTERFACE):
        value = getattr(billing_services, name)
        signature = inspect.signature(value)
        contract[name] = {
            "module": value.__module__,
            "return_annotation": str(signature.return_annotation),
            "signature": str(signature),
        }
    return contract


def test_billing_services_occurrences_match_migration_allowlist():
    expected = json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8"))
    assert build_occurrence_manifest() == expected


def test_public_facade_contract_matches_frozen_signatures_and_owners():
    expected = json.loads(PUBLIC_CONTRACT_PATH.read_text(encoding="utf-8"))
    assert build_public_interface_contract() == expected


def test_public_facade_is_explicit_and_expenses_resolve_to_owner():
    assert set(billing_services.__all__) == PUBLIC_INTERFACE
    assert billing_services.create_expense is expenses.create_expense
    assert billing_services.update_expense is expenses.update_expense
    assert billing_services.delete_expense is expenses.delete_expense


def test_catalog_public_facade_names_resolve_to_owner():
    for name in (
        "create_discount",
        "create_tariff",
        "create_training_type",
        "is_training_type_kind_locked",
        "update_discount",
        "update_tariff",
        "update_training_type",
    ):
        assert getattr(billing_services, name) is getattr(catalog, name)


def test_slice_two_private_symbols_have_exact_owners():
    catalog_symbols = {
        "_has_open_personal_drop_in_for_contract_change",
        "_resolve_grade_system_id_for_training_type",
        "_validate_discount_value",
        "_validate_training_type_kind",
    }
    component_symbols = MOVED_PRIVATE_IMPLEMENTATIONS[2] - catalog_symbols
    for name in catalog_symbols:
        assert getattr(catalog, name).__module__ == catalog.__name__
    for name in component_symbols:
        assert getattr(tariff_components, name).__module__ == tariff_components.__name__


def test_slice_three_private_symbols_have_exact_owner():
    for name in MOVED_PRIVATE_IMPLEMENTATIONS[3]:
        assert getattr(entitlements, name).__module__ == entitlements.__name__


def test_slice_four_debt_symbols_have_exact_owner():
    debt_symbols = {
        "_assert_personal_drop_in_payment_path",
        "_attach_debts_to_subscription",
        "_attach_subscription_to_pending_payment_debts",
        "_closed_period_personal_drop_in_debt_ids",
        "_personal_drop_in_bookings_by_debt",
        "_reserve_debts_for_payment",
        "_validate_personal_drop_in_debt_tariff_contract",
        "assert_payment_reservation_capacity",
        "debt_state",
        "record_debt_lifecycle_event",
        "record_debt_settlement_events",
        "write_off_debt",
    }
    for name in debt_symbols:
        assert getattr(debts, name).__module__ == debts.__name__
    assert billing_services.write_off_debt is debts.write_off_debt


def test_slice_five_group_payment_symbols_have_exact_owner():
    for name in MOVED_PRIVATE_IMPLEMENTATIONS[5]:
        assert not hasattr(billing_services, name)
        assert getattr(group_payments, name).__module__ == group_payments.__name__


def test_slice_six_subscription_and_sale_earning_symbols_have_exact_owners():
    subscription_symbols = {
        "_validate_direct_subscription_payment_method",
        "create_subscription",
    }
    sale_earning_symbols = MOVED_PRIVATE_IMPLEMENTATIONS[6] - subscription_symbols
    for name in subscription_symbols:
        if name.startswith("_"):
            assert not hasattr(billing_services, name)
        assert getattr(subscriptions, name).__module__ == subscriptions.__name__
    for name in sale_earning_symbols:
        assert not hasattr(billing_services, name)
        assert getattr(sale_earnings, name).__module__ == sale_earnings.__name__
    assert billing_services.create_subscription is subscriptions.create_subscription


def test_slice_seven_payment_creation_symbols_have_exact_owner():
    for name in MOVED_PRIVATE_IMPLEMENTATIONS[7]:
        if name.startswith("_"):
            assert not hasattr(billing_services, name)
        assert getattr(payment_creation, name).__module__ == payment_creation.__name__
    assert billing_services.create_payment is payment_creation.create_payment


def test_slice_eight_payment_review_symbol_has_exact_owner():
    assert payment_review.verify_payment.__module__ == payment_review.__name__


def test_slice_nine_club_settings_symbols_have_exact_owner():
    for name in MOVED_PRIVATE_IMPLEMENTATIONS[9]:
        assert getattr(club_settings, name).__module__ == club_settings.__name__


def test_slice_ten_freeze_symbols_have_exact_owner():
    for name in MOVED_PRIVATE_IMPLEMENTATIONS[10]:
        if name.startswith("_"):
            assert not hasattr(billing_services, name)
        assert getattr(freezes, name).__module__ == freezes.__name__


def test_slice_eleven_bank_order_symbols_have_exact_owner():
    for name in MOVED_PRIVATE_IMPLEMENTATIONS[11]:
        if name.startswith("_"):
            assert not hasattr(billing_services, name)
        assert getattr(bank_orders, name).__module__ == bank_orders.__name__


def test_slice_twelve_provider_and_review_symbols_have_exact_owners():
    review_symbols = {
        "_clean_review_reason",
        "_mark_order_manual_review",
        "_refund_case_for_manual_review",
        "_sanitize_review_evidence",
        "resolve_bank_payment_order_manual_review",
    }
    for name in review_symbols:
        if name.startswith("_"):
            assert not hasattr(billing_services, name)
        assert getattr(bank_order_review, name).__module__ == bank_order_review.__name__
    for name in MOVED_PRIVATE_IMPLEMENTATIONS[12] - review_symbols:
        if name.startswith("_"):
            assert not hasattr(billing_services, name)
        assert getattr(provider_events, name).__module__ == provider_events.__name__


def test_slice_fourteen_renewal_symbols_have_exact_owner():
    for name in MOVED_PRIVATE_IMPLEMENTATIONS[14]:
        assert not hasattr(billing_services, name)
        assert getattr(renewals, name).__module__ == renewals.__name__


def test_attendance_uses_explicit_debt_seam_not_private_facade_helpers():
    private_helpers = {
        "_assert_payment_reservation_capacity",
        "_debt_state",
        "_record_debt_lifecycle_event",
        "_record_debt_settlement_events",
    }
    for relative in (
        "apps/attendance/services/checkin.py",
        "apps/attendance/services/drop_in.py",
    ):
        occurrences = _python_occurrences(ROOT / relative)
        assert all(("import-from", helper) not in occurrences for helper in private_helpers)


def test_completed_slice_private_implementations_are_absent_from_facade():
    facade_path = ROOT / "apps" / "billing" / "services.py"
    source = facade_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(facade_path))
    facade_definitions = {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    overdue = set().union(
        *(
            symbols
            for slice_number, symbols in MOVED_PRIVATE_IMPLEMENTATIONS.items()
            if slice_number <= CURRENT_MIGRATION_SLICE
        )
    )
    assert facade_definitions.isdisjoint(overdue)


def test_final_facade_has_no_lifecycle_implementation():
    facade_path = ROOT / "apps" / "billing" / "services.py"
    tree = ast.parse(facade_path.read_text(encoding="utf-8"), filename=str(facade_path))
    forbidden = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    assert not any(isinstance(node, forbidden) for node in tree.body)
    assignments = [
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    ]
    assert assignments == ["__all__"]
    assert set(billing_services.__all__) == PUBLIC_INTERFACE


def test_unified_client_journey_contract_preserves_existing_financial_owners():
    from apps.students.journey_contracts import (
        EXISTING_MUTATION_OWNERS,
        verify_existing_mutation_owners,
    )

    assert EXISTING_MUTATION_OWNERS == {
        "group_manual": ("apps.billing.service_modules.payment_creation", "create_payment"),
        "group_and_renewal_online": (
            "apps.billing.service_modules.bank_orders",
            "create_bank_payment_order",
        ),
        "manual_review": ("apps.billing.service_modules.payment_review", "verify_payment"),
        "personal_entitlement": ("apps.attendance.services.enrollment", "book_personal_session"),
        "personal_online_reservation": (
            "apps.attendance.services.enrollment",
            "create_personal_booking_payment_reservation",
        ),
        "personal_online_bank_order": (
            "apps.billing.service_modules.bank_orders",
            "create_bank_payment_order",
        ),
        "pay_at_visit": ("apps.attendance.services.drop_in", "book_personal_drop_in"),
    }
    assert verify_existing_mutation_owners() is True


def test_service_module_dependency_graph_is_acyclic_and_uses_exact_owners():
    internal_root = ROOT / "apps" / "billing" / "service_modules"
    module_names = {path.stem for path in internal_root.glob("*.py")}
    graph = {name: set() for name in module_names}
    package_root_imports = []
    dynamic_internal_edges: set[tuple[str, str]] = set()

    for path in internal_root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module == "apps.billing.service_modules":
                    package_root_imports.append(path.relative_to(ROOT).as_posix())
                elif node.module and node.module.startswith(
                    "apps.billing.service_modules."
                ):
                    target = node.module.rsplit(".", maxsplit=1)[-1]
                    if target in module_names:
                        graph[path.stem].add(target)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    prefix = "apps.billing.service_modules."
                    if alias.name.startswith(prefix):
                        target = alias.name.removeprefix(prefix).split(".", maxsplit=1)[0]
                        if target in module_names:
                            graph[path.stem].add(target)
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "import_module"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and node.args[0].value.startswith("apps.billing.service_modules.")
            ):
                target = node.args[0].value.rsplit(".", maxsplit=1)[-1]
                if target in module_names:
                    dynamic_internal_edges.add((path.stem, target))

    assert package_root_imports == []
    assert dynamic_internal_edges == {
        ("bank_orders", "provider_events"),
        ("renewals", "payment_creation"),
    }

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(module: str, path: tuple[str, ...] = ()) -> None:
        assert module not in visiting, " -> ".join((*path, module))
        if module in visited:
            return
        visiting.add(module)
        for dependency in sorted(graph[module]):
            visit(dependency, (*path, module))
        visiting.remove(module)
        visited.add(module)

    for module in sorted(graph):
        visit(module)


def test_final_slice_has_no_overdue_production_service_seams():
    overdue = [
        row
        for row in build_occurrence_manifest()
        if int(row["migration_slice"]) < CURRENT_MIGRATION_SLICE
        and not str(row["path"]).startswith("apps/billing/tests/")
    ]
    assert overdue == []


def test_internal_service_modules_do_not_import_facade():
    internal_root = ROOT / "apps" / "billing" / "service_modules"
    offenders = []
    for path in internal_root.glob("*.py"):
        occurrences = _python_occurrences(path)
        if occurrences:
            offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []


def test_fresh_process_facade_import_does_not_open_database_connection():
    code = """
import django
import importlib
from unittest import mock
from django.db.backends.base.base import BaseDatabaseWrapper

with mock.patch.object(
    BaseDatabaseWrapper,
    "ensure_connection",
    side_effect=AssertionError("billing service import accessed the database"),
):
    django.setup()
    services = importlib.import_module(".".join(("apps", "billing", "services")))
    print(",".join(sorted(services.__all__)))
"""
    env = os.environ.copy()
    env.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.test")
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip().split(",") == sorted(PUBLIC_INTERFACE)


if __name__ == "__main__":
    payload = (
        build_public_interface_contract()
        if "--public-contract" in sys.argv
        else build_occurrence_manifest()
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
