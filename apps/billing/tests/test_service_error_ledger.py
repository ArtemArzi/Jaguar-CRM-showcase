from __future__ import annotations

import ast
import importlib
import json
from pathlib import Path

import apps.billing.services as billing_services

ROOT = Path(__file__).resolve().parents[3]
LEDGER_PATH = Path(__file__).with_name("service_error_contract_ledger.json")
PUBLIC_INTERFACE_PATH = Path(__file__).with_name("public_interface_contract.json")
OWNER_MODULES = (
    "apps.billing.service_modules._shared",
    "apps.billing.service_modules.catalog",
    "apps.billing.service_modules.debts",
    "apps.billing.service_modules.entitlements",
    "apps.billing.service_modules.expenses",
    "apps.billing.service_modules.tariff_components",
)


def _ledger() -> dict:
    return json.loads(LEDGER_PATH.read_text(encoding="utf-8"))


def _module_tree(module_name: str) -> ast.Module:
    path = ROOT / Path(*module_name.split(".")).with_suffix(".py")
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _business_logic_raises() -> dict[str, list[tuple[str, str]]]:
    result: dict[str, list[tuple[str, str]]] = {}
    for module_name in OWNER_MODULES:
        for node in _module_tree(module_name).body:
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            raises = []
            for candidate in ast.walk(node):
                if not isinstance(candidate, ast.Raise):
                    continue
                exception = candidate.exc
                if (
                    not isinstance(exception, ast.Call)
                    or not isinstance(exception.func, ast.Name)
                    or exception.func.id != "BusinessLogicError"
                    or not exception.args
                    or not isinstance(exception.args[0], ast.Constant)
                ):
                    continue
                code = next(
                    (
                        keyword.value.value
                        for keyword in exception.keywords
                        if keyword.arg == "code"
                        and isinstance(keyword.value, ast.Constant)
                        and isinstance(keyword.value.value, str)
                    ),
                    None,
                )
                assert code is not None, f"{module_name}:{node.name} has an unledgerable error code"
                raises.append((code, str(exception.args[0].value)))
            if raises:
                result[f"{module_name}:{node.name}"] = sorted(raises)
    return result


def _evidence_node(node_id: str) -> ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef | None:
    path_text, *parts = node_id.split("::")
    path = ROOT / path_text
    if not path.is_file() or not parts:
        return None
    nodes = _module_tree_from_path(path)
    current = nodes
    for part in parts:
        match = next(
            (
                node
                for node in current
                if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
                and node.name == part
            ),
            None,
        )
        if match is None:
            return None
        current = match.body
    return match


def _evidence_node_exists(node_id: str) -> bool:
    return _evidence_node(node_id) is not None


def _called_function_names(
    node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
) -> set[str]:
    calls = set()
    for candidate in ast.walk(node):
        if not isinstance(candidate, ast.Call):
            continue
        if isinstance(candidate.func, ast.Name):
            calls.add(candidate.func.id)
        elif isinstance(candidate.func, ast.Attribute):
            calls.add(candidate.func.attr)
    return calls


def _module_tree_from_path(path: Path) -> list[ast.stmt]:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path)).body


def test_error_ledger_matches_every_direct_business_logic_raise_through_slice_four():
    expected = {
        source: sorted(
            (branch["code"], branch["message"])
            for branch in branches
        )
        for source, branches in _ledger()["direct_raises"].items()
    }
    assert _business_logic_raises() == expected


def test_error_ledger_has_separate_complete_public_interface_entries():
    ledger = _ledger()
    assert ledger["completed_slice"] == 4
    expected_interfaces = {
        "create_discount",
        "create_expense",
        "create_tariff",
        "create_training_type",
        "assert_payment_reservation_capacity",
        "debt_state",
        "delete_expense",
        "is_training_type_kind_locked",
        "record_debt_lifecycle_event",
        "record_debt_settlement_events",
        "update_discount",
        "update_expense",
        "update_tariff",
        "update_training_type",
        "write_off_debt",
    }
    assert set(ledger["interfaces"]) == expected_interfaces

    for name, contract in ledger["interfaces"].items():
        owner = importlib.import_module(contract["owner"])
        owner_interface = getattr(owner, name)
        if contract.get("exposed_via_facade", True):
            assert getattr(billing_services, name) is owner_interface
        else:
            assert name in {
                "assert_payment_reservation_capacity",
                "debt_state",
                "record_debt_lifecycle_event",
                "record_debt_settlement_events",
            }
            public_interface = json.loads(PUBLIC_INTERFACE_PATH.read_text(encoding="utf-8"))
            assert name not in public_interface
            assert owner_interface.__module__ == contract["owner"]
        assert set(contract["failure_state"]) == {
            "audit_before_failure",
            "mutations_before_failure",
            "rollback",
        }
        assert all(
            isinstance(value, str) and value
            for value in contract["failure_state"].values()
        )
        assert isinstance(contract["implicit"], list)
        for branch in contract["implicit"]:
            assert {
                "audit_before_failure",
                "exception",
                "mutations_before_failure",
                "rollback",
                "trigger",
            } <= set(branch)
            assert all(
                isinstance(branch[field], str) and branch[field]
                for field in (
                    "audit_before_failure",
                    "exception",
                    "mutations_before_failure",
                    "rollback",
                    "trigger",
                )
            )
            evidence_kinds = {"evidence", "unreachable_proof"} & set(branch)
            assert len(evidence_kinds) == 1
            evidence_kind = evidence_kinds.pop()
            if evidence_kind == "evidence":
                assert _evidence_node_exists(branch["evidence"]), branch["evidence"]
            else:
                assert (
                    isinstance(branch["unreachable_proof"], str)
                    and branch["unreachable_proof"]
                )
        for source in contract["error_sources"]:
            assert source in ledger["direct_raises"]

    debt_interfaces = {
        "assert_payment_reservation_capacity",
        "debt_state",
        "record_debt_lifecycle_event",
        "record_debt_settlement_events",
        "write_off_debt",
    }
    for name in debt_interfaces:
        return_contract = ledger["interfaces"][name]["return_contract"]
        assert {
            "evidence",
            "identity",
        } == set(return_contract)
        evidence_node = _evidence_node(return_contract["evidence"])
        assert evidence_node is not None
        assert name in _called_function_names(evidence_node), (
            f"{return_contract['evidence']} does not call {name}"
        )
        assert isinstance(return_contract["identity"], str) and return_contract["identity"]


def test_every_direct_error_branch_points_to_an_existing_test_node():
    for branches in _ledger()["direct_raises"].values():
        for branch in branches:
            assert _evidence_node_exists(branch["evidence"]), branch["evidence"]


def test_slice_four_debt_direct_error_evidence_has_exact_branch_markers_and_state_contract():
    ledger = _ledger()
    state_fields = {"audit_before_failure", "mutations_before_failure", "rollback"}
    debt_sources = {
        source: branches
        for source, branches in ledger["direct_raises"].items()
        if source.startswith("apps.billing.service_modules.debts:")
    }
    assert debt_sources

    for source, branches in debt_sources.items():
        for branch in branches:
            assert state_fields <= set(branch), f"{source}:{branch['code']} has no failure-state contract"
            assert all(isinstance(branch[field], str) and branch[field] for field in state_fields)
            node = _evidence_node(branch["evidence"])
            assert node is not None, branch["evidence"]
            source_function = source.rsplit(":", 1)[1]
            assert source_function in _called_function_names(node), (
                f"{branch['evidence']} does not call {source_function}"
            )
            literals = {
                value.value
                for value in ast.walk(node)
                if isinstance(value, ast.Constant) and isinstance(value.value, str)
            }
            assert branch["code"] in literals, f"{branch['evidence']} lacks {branch['code']}"
            assert branch["message"] in literals, f"{branch['evidence']} lacks {branch['message']}"


def test_slice_four_debt_interface_implicit_error_evidence_has_exact_markers_and_state_contract():
    ledger = _ledger()
    state_fields = {"audit_before_failure", "mutations_before_failure", "rollback"}
    debt_interfaces = {
        "assert_payment_reservation_capacity",
        "debt_state",
        "record_debt_lifecycle_event",
        "record_debt_settlement_events",
        "write_off_debt",
    }

    for interface_name in debt_interfaces:
        for branch in ledger["interfaces"][interface_name]["implicit"]:
            assert state_fields <= set(branch)
            assert all(isinstance(branch[field], str) and branch[field] for field in state_fields)
            node = _evidence_node(branch["evidence"])
            assert node is not None, branch["evidence"]
            assert interface_name in _called_function_names(node), (
                f"{branch['evidence']} does not call {interface_name}"
            )
            literals = {
                value.value
                for value in ast.walk(node)
                if isinstance(value, ast.Constant) and isinstance(value.value, str)
            }
            if branch["exception"] == "apps.common.exceptions.BusinessLogicError":
                assert branch["code"] in literals
                assert branch["message_pattern"] in literals
                continue

            assert "message" in branch, (
                f"{interface_name}:{branch['trigger']} has no exact message contract"
            )
            marker = branch.get("message_marker", branch["message"])
            assert any(marker in literal for literal in literals), (
                f"{branch['evidence']} lacks exact message marker {marker!r}"
            )
