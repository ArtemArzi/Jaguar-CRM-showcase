import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIPPED_PARTS = {"migrations", "tests", "__pycache__"}
CODE_PATTERNS = ("apps/**/*.py", "config/**/*.py")
MASS_ASSIGNMENT_PATTERNS = ("apps/**/api.py", "apps/htmx_admin/views/**/*.py")
REQUEST_SCHEMA_NAMES = {"data", "payload"}
RAW_SQL_ALLOWLIST = {
    ("config/api.py", "connection.cursor()"): "Readiness check uses constant SELECT 1.",
    ("config/api.py", 'cursor.execute("SELECT 1")'): "Readiness check uses constant SELECT 1.",
}
UNSCOPED_ALLOWLIST = {
    (
        "apps/billing/models.py",
        "Tariff.objects.unscoped()",
    ): (
        "Component integrity checks lock exact parent IDs across clubs before rejecting foreign links "
        "or changes to revision-sealed tariffs; the supplied club cannot hide a persisted parent."
    ),
    (
        "apps/billing/models.py",
        "type(self).objects.unscoped()",
    ): (
        "Tariff and component save guards reload persisted rows by primary key before validating "
        "ownership and revision seals; a tampered in-memory club must not conceal the protected row."
    ),
    (
        "apps/students/imports/tasks.py",
        "OpeningImportBatch.objects.unscoped()",
    ): (
        "Scheduled private-file cleanup must preserve applying batches from every club; "
        "it reads file IDs only and never mutates tenant or financial records."
    ),
    (
        "apps/billing/service_modules/provider_events.py",
        "BankPaymentOrder.objects.unscoped()",
    ): (
        "Webhook lookup and scheduled create recovery locate orders before trusted club context; "
        "each mutation then uses stored tenant context."
    ),
    (
        "apps/billing/service_modules/provider_events.py",
        "BankPaymentProviderEvent.objects.unscoped()",
    ): "Provider event idempotency is intentionally cross-tenant.",
    (
        "apps/billing/service_modules/provider_events.py",
        "BankPaymentReconciliationAttempt.objects.unscoped()",
    ): "Scheduled reconciliation enumerates due attempts across clubs before each club-scoped claim.",
    (
        "apps/billing/service_modules/bank_orders.py",
        "BankPaymentOrder.objects.unscoped()",
    ): (
        "Expiry scans enumerate all clubs and provider operation identifiers are conflict-checked globally; "
        "each mutation then uses stored tenant context."
    ),
    (
        "config/urls.py",
        "BankPaymentOrder.objects.unscoped()",
    ): "Local-only mock checkout resolves an opaque provider link before club context exists.",
    (
        "apps/billing/management/commands/audit_operational_admissions.py",
        "Debt.objects.unscoped()",
    ): "Aggregate rollout audit detects foreign-club debt links to one explicitly scoped club's payments.",
    (
        "apps/billing/management/commands/audit_operational_admissions.py",
        "DebtSettlementEvent.objects.unscoped()",
    ): "Aggregate rollout audit validates the latest lifecycle of foreign-linked current reservations.",
    (
        "apps/billing/service_modules/catalog.py",
        "TrainingType.objects.unscoped()",
    ): (
        "Tariff update locks the exact training-type FK from an already tenant-scoped tariff; "
        "legacy cross-club references remain readable while every tariff mutation stays club-scoped."
    ),
    (
        "apps/attendance/models.py",
        "objects.unscoped()",
    ): (
        "Append-only payment-correction validation must load the persisted row by primary key even when "
        "an in-memory object has been tampered to another club; the save then rejects every tenant change."
    ),
    (
        "apps/students/management/commands/audit_student_provenance.py",
        "Student.objects.unscoped()",
    ): "Read-only management audit aggregates student provenance across all clubs when --all-clubs is explicit.",
    (
        "apps/students/management/commands/audit_student_provenance.py",
        "StudentProvenanceBackfillReceipt.objects.unscoped()",
    ): "Read-only management audit aggregates append-only provenance receipts across explicitly requested clubs.",
    (
        "apps/students/management/commands/audit_student_provenance.py",
        "LeadLifecycleEvent.objects.unscoped()",
    ): "Read-only management audit reconciles lifecycle evidence across all clubs without mutating lead state.",
    (
        "apps/common/management/commands/prepare_unified_client_commercial_journey_e2e.py",
        "Student.objects.unscoped()",
    ): (
        "Isolated E2E setup checks fixture-phone candidates across clubs before creating its own club; "
        "a reused student phone can later collide with the global account-access username."
    ),
    ("apps/billing/tasks.py", "SubscriptionFreeze.objects.unscoped()"): "Cron expires freezes across all clubs.",
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


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _is_allowlisted(rel_path: str, source: str, allowlist: dict[tuple[str, str], str]) -> bool:
    return any(path == rel_path and snippet in source for path, snippet in allowlist)


def _chain_parts(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Attribute):
        return [*_chain_parts(node.value), node.attr]
    if isinstance(node, ast.Call):
        return _chain_parts(node.func)
    return []


def _is_schema_dump_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr not in {"dict", "model_dump"}:
        return False
    return isinstance(node.func.value, ast.Name) and node.func.value.id in REQUEST_SCHEMA_NAMES


def _is_schema_dump_items_call(node: ast.AST, raw_dump_names: set[str]) -> bool:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr != "items":
        return False
    value = node.func.value
    if _is_schema_dump_call(value):
        return True
    return isinstance(value, ast.Name) and value.id in raw_dump_names


def _assigned_names(target: ast.AST) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        names: set[str] = set()
        for item in target.elts:
            names.update(_assigned_names(item))
        return names
    return set()


def _raw_dump_names(function_node: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(function_node):
        if isinstance(node, ast.Assign) and _is_schema_dump_call(node.value):
            for target in node.targets:
                names.update(_assigned_names(target))
        elif isinstance(node, ast.AnnAssign) and node.value is not None and _is_schema_dump_call(node.value):
            names.update(_assigned_names(node.target))
    return names


def test_raw_sql_usage_is_allowlisted():
    violations = []
    for path in _iter_python_files(CODE_PATTERNS):
        rel_path = _rel(path)
        source = path.read_text()
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            parts = _chain_parts(node.func)
            if parts[-2:] not in (["connection", "cursor"], ["cursor", "execute"], ["cursor", "executemany"]):
                if not parts or parts[-1] not in {"raw", "extra", "RawSQL"}:
                    continue
            segment = ast.get_source_segment(source, node) or ""
            if _is_allowlisted(rel_path, segment, RAW_SQL_ALLOWLIST):
                continue
            violations.append(f"{rel_path}:{node.lineno} - raw SQL usage must be allowlisted: {segment.strip()}")

    assert not violations, "Raw SQL usages found:\n" + "\n".join(violations)


def test_unscoped_usage_is_allowlisted():
    violations = []
    for path in _iter_python_files(CODE_PATTERNS):
        rel_path = _rel(path)
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if line.strip().startswith("#"):
                continue
            if ".unscoped()" not in line:
                continue
            if _is_allowlisted(rel_path, line, UNSCOPED_ALLOWLIST):
                continue
            violations.append(f"{rel_path}:{lineno} - .unscoped() must be allowlisted with a cross-tenant reason")

    assert not violations, "Unallowlisted .unscoped() usages found:\n" + "\n".join(violations)


def test_no_whole_request_schema_dump_used_for_mutation_kwargs_or_setattr_loop():
    violations = []
    for path in _iter_python_files(MASS_ASSIGNMENT_PATTERNS):
        rel_path = _rel(path)
        source = path.read_text()
        tree = ast.parse(source, filename=str(path))
        for function_node in [
            node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]:
            raw_dump_names = _raw_dump_names(function_node)
            for node in ast.walk(function_node):
                if isinstance(node, ast.Call):
                    for keyword in node.keywords:
                        if keyword.arg is not None:
                            continue
                        if _is_schema_dump_call(keyword.value) or (
                            isinstance(keyword.value, ast.Name) and keyword.value.id in raw_dump_names
                        ):
                            segment = ast.get_source_segment(source, node) or ""
                            violations.append(
                                f"{rel_path}:{node.lineno} - do not expand raw request schema dumps into "
                                f"mutation calls: {segment.strip()}"
                            )
                if isinstance(node, ast.For) and _is_schema_dump_items_call(node.iter, raw_dump_names):
                    segment = ast.get_source_segment(source, node) or ""
                    violations.append(
                        f"{rel_path}:{node.lineno} - do not loop over raw request schema dumps for "
                        f"setattr: {segment.strip()}"
                    )

    assert not violations, "Whole request schema dump mutation paths found:\n" + "\n".join(violations)
