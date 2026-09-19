import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIPPED_PARTS = {"migrations", "tests", "__pycache__"}
TENANT_MODEL_PATTERNS = ("apps/**/models.py",)
TENANT_QUERY_PATTERNS = (
    "apps/**/selectors.py",
    "apps/**/services.py",
    "apps/**/services/*.py",
    "apps/**/api.py",
    "apps/**/tasks.py",
    "apps/htmx_admin/views/**/*.py",
)
TENANT_QUERY_METHODS = {
    "all",
    "exclude",
    "filter",
    "get",
    "get_or_create",
    "select_for_update",
    "update_or_create",
    "values",
    "values_list",
}
TENANT_SCOPE_KWARGS = {"club", "club_id"}
TENANT_QUERY_ALLOWLIST = {
    (
        "apps/students/parent_services.py",
        "ParentInvite",
        "select_for_update",
    ): "Invite-token lookup starts before a trusted club context exists.",
    (
        "apps/students/parent_services.py",
        "ParentInvite",
        "get",
    ): "Invite-token lookup starts before a trusted club context exists.",
}


def _iter_python_files(patterns: tuple[str, ...]):
    seen: set[Path] = set()
    for pattern in patterns:
        for path in ROOT.glob(pattern):
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            if SKIPPED_PARTS.intersection(path.relative_to(ROOT).parts):
                continue
            yield path


def _chain_parts(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Attribute):
        return [*_chain_parts(node.value), node.attr]
    if isinstance(node, ast.Call):
        return _chain_parts(node.func)
    return []


def _inherits_tenant_mixin(node: ast.ClassDef) -> bool:
    return any(_chain_parts(base)[-1:] == ["TenantMixin"] for base in node.bases)


def _tenant_model_names() -> set[str]:
    models: set[str] = set()
    for path in _iter_python_files(TENANT_MODEL_PATTERNS):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and _inherits_tenant_mixin(node):
                models.add(node.name)
    return models


def _tenant_query_for_call(node: ast.Call, tenant_models: set[str]) -> tuple[str, str] | None:
    if not isinstance(node.func, ast.Attribute):
        return None
    method = node.func.attr
    if method not in TENANT_QUERY_METHODS:
        return None
    parts = _chain_parts(node.func)
    if len(parts) < 3 or parts[1] != "objects" or parts[0] not in tenant_models:
        return None
    return parts[0], method


def _call_chain_calls(node: ast.AST) -> list[ast.Call]:
    calls = []
    if isinstance(node, ast.Call):
        calls.append(node)
        calls.extend(_call_chain_calls(node.func))
    elif isinstance(node, ast.Attribute):
        calls.extend(_call_chain_calls(node.value))
    return calls


def _has_tenant_scope(node: ast.Call) -> bool:
    parts = _chain_parts(node.func)
    if "for_club" in parts or "unscoped" in parts:
        return True
    return any(
        keyword.arg in TENANT_SCOPE_KWARGS
        for call in _call_chain_calls(node)
        for keyword in call.keywords
    )


def test_all_tenant_models_are_discovered_from_tenant_mixin():
    tenant_models = _tenant_model_names()

    assert "SubscriptionFreeze" in tenant_models
    assert "BankPaymentOrder" in tenant_models
    assert "ScheduleBookingEvent" in tenant_models
    assert "PushSubscription" not in tenant_models


def test_tenant_model_queries_are_scoped_or_explicitly_allowlisted():
    tenant_models = _tenant_model_names()
    violations = []

    for path in _iter_python_files(TENANT_QUERY_PATTERNS):
        rel_path = path.relative_to(ROOT).as_posix()
        source = path.read_text()
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            query = _tenant_query_for_call(node, tenant_models)
            if query is None:
                continue
            model_name, method = query
            if _has_tenant_scope(node):
                continue
            allow_reason = TENANT_QUERY_ALLOWLIST.get((rel_path, model_name, method))
            if allow_reason:
                continue
            source_line = (ast.get_source_segment(source, node) or "").strip().splitlines()[0]
            violations.append(
                f"{rel_path}:{node.lineno} - {model_name}.objects.{method} lacks tenant scope: "
                f"{source_line}"
            )

    assert not violations, "Unscoped tenant model queries found:\n" + "\n".join(violations)
