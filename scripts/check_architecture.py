"""Architecture guardrails checker (IMP-0002).

Statically enforces the import boundaries and forbidden patterns required by
the Technology and Deployment specification (section 4) and the Security and
Operations specification (section 6):

- provider SDKs are allowed only inside their adapter packages
  (ibapi only inside packages/py/broker-ibkr);
- contracts import no internal packages; domain imports only contracts;
- the broker adapter is imported only by itself and the execution agent;
- no hardcoded account identifiers, no LIVE defaults, no queue names mixing
  paper and live, no obvious secrets, no hardcoded Windows paths.

Pure standard library (ast, re, pathlib) so it runs identically on Windows
and Linux ARM64. Exit code 0 means compliant; 1 means violations found.

Usage: uv run python scripts/check_architecture.py [repo_root]
"""

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

SCAN_ROOTS: tuple[str, ...] = ("apps", "packages/py")

# External SDK -> repo-relative POSIX prefixes where importing it is allowed.
# An empty tuple means the SDK is forbidden in all scanned Python code.
SDK_ALLOWED_PREFIXES: dict[str, tuple[str, ...]] = {
    "ibapi": ("packages/py/broker-ibkr",),
    "fastapi": ("apps/api",),
    "sqlalchemy": ("packages/py/persistence",),
    "alembic": ("packages/py/persistence",),
    "nats": ("packages/py/event-bus",),
    "playwright": (),
}

INTERNAL_PACKAGES: frozenset[str] = frozenset(
    {
        "trading_api",
        "trading_core_worker",
        "trading_scheduler",
        "trading_execution_agent",
        "trading_contracts",
        "trading_domain",
        "trading_application",
        "trading_config",
        "trading_persistence",
        "trading_event_bus",
        "trading_market_data_core",
        "trading_broker_core",
        "trading_broker_ibkr",
        "trading_observability",
    }
)

# Source prefix -> internal packages that code under the prefix may import
# (in addition to its own package). Prefixes not listed here are unrestricted
# at the internal-import level except for the broker adapter rule below.
INTERNAL_ALLOWED: dict[str, frozenset[str]] = {
    "packages/py/contracts": frozenset(),
    "packages/py/domain": frozenset({"trading_contracts"}),
}

OWN_PACKAGE_BY_PREFIX: dict[str, str] = {
    "packages/py/contracts": "trading_contracts",
    "packages/py/domain": "trading_domain",
}

# The concrete broker adapter may be imported only by itself and by the
# execution agent runtime (Technology spec: adapters depend on core
# interfaces; nothing else depends on adapters).
BROKER_ADAPTER_MODULE = "trading_broker_ibkr"
BROKER_ADAPTER_ALLOWED_PREFIXES: tuple[str, ...] = (
    "packages/py/broker-ibkr",
    "apps/execution-agent",
)

# Additional modules the pure domain layer must never import
# (Technology spec section 4: no ibapi, Playwright, NATS client or FastAPI
# in domain; domain stays free of I/O clients).
DOMAIN_FORBIDDEN_EXTERNAL: frozenset[str] = frozenset({"httpx", "requests"})
DOMAIN_PREFIXES: tuple[str, ...] = ("packages/py/domain", "packages/py/contracts")

FORBIDDEN_TEXT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "hardcoded broker account id",
        re.compile(r"[\"'](?:DU|U)\d{6,8}[\"']"),
    ),
    (
        "LIVE used as a default environment",
        re.compile(r"(?i)\b(?:default|environment)\s*[:=]\s*[\"']LIVE[\"']"),
    ),
    (
        "queue/topic name mixing paper and live",
        re.compile(
            r"[\"'][\w.\-/]*(?:paper[\w.\-/]*live|live[\w.\-/]*paper)[\w.\-/]*[\"']",
            re.IGNORECASE,
        ),
    ),
    (
        "private key material",
        re.compile(r"-----BEGIN(?: [A-Z]+)? PRIVATE KEY-----"),
    ),
    (
        "AWS-style access key id",
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    ),
    (
        "hardcoded credential assignment",
        re.compile(r"(?i)\b(?:api_key|secret_key|access_token)\s*=\s*[\"'][A-Za-z0-9+/]{20,}[\"']"),
    ),
    (
        "hardcoded Windows path",
        re.compile(r"[\"'][A-Za-z]:\\"),
    ),
)


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    rule: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: [{self.rule}] {self.message}"


EXCLUDED_DIR_NAMES: frozenset[str] = frozenset(
    {"node_modules", ".venv", "dist", ".next", "__pycache__", "coverage"}
)


def _iter_python_files(repo_root: Path) -> list[Path]:
    files: list[Path] = []
    for scan_root in SCAN_ROOTS:
        root = repo_root / Path(scan_root)
        if not root.is_dir():
            continue
        for file_path in sorted(root.rglob("*.py")):
            relative_parts = file_path.relative_to(root).parts
            if not EXCLUDED_DIR_NAMES.intersection(relative_parts):
                files.append(file_path)
    return files


def _under(rel_path: str, prefix: str) -> bool:
    return rel_path == prefix or rel_path.startswith(prefix + "/")


def _top_level_imports(tree: ast.AST) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name.split(".")[0], node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module is not None:
                found.append((node.module.split(".")[0], node.lineno))
    return found


def _check_imports(rel_path: str, tree: ast.AST) -> list[Violation]:
    violations: list[Violation] = []
    for module, line in _top_level_imports(tree):
        allowed_prefixes = SDK_ALLOWED_PREFIXES.get(module)
        if allowed_prefixes is not None and not any(
            _under(rel_path, prefix) for prefix in allowed_prefixes
        ):
            violations.append(
                Violation(
                    rel_path,
                    line,
                    "sdk-boundary",
                    f"provider SDK {module!r} is allowed only under "
                    f"{list(allowed_prefixes) or 'no path (forbidden in Python)'}",
                )
            )
        if module == BROKER_ADAPTER_MODULE and not any(
            _under(rel_path, prefix) for prefix in BROKER_ADAPTER_ALLOWED_PREFIXES
        ):
            violations.append(
                Violation(
                    rel_path,
                    line,
                    "broker-adapter-boundary",
                    f"{BROKER_ADAPTER_MODULE!r} may be imported only by "
                    f"{list(BROKER_ADAPTER_ALLOWED_PREFIXES)}",
                )
            )
        for prefix, allowed_internal in INTERNAL_ALLOWED.items():
            if not _under(rel_path, prefix) or module not in INTERNAL_PACKAGES:
                continue
            if module == OWN_PACKAGE_BY_PREFIX[prefix] or module in allowed_internal:
                continue
            violations.append(
                Violation(
                    rel_path,
                    line,
                    "layer-boundary",
                    f"code under {prefix!r} may not import internal package "
                    f"{module!r} (allowed: {sorted(allowed_internal) or 'none'})",
                )
            )
        if module in DOMAIN_FORBIDDEN_EXTERNAL and any(
            _under(rel_path, prefix) for prefix in DOMAIN_PREFIXES
        ):
            violations.append(
                Violation(
                    rel_path,
                    line,
                    "domain-purity",
                    f"I/O client {module!r} is forbidden in domain/contracts layers",
                )
            )
    return violations


def _check_text(rel_path: str, text: str) -> list[Violation]:
    violations: list[Violation] = []
    for line_number, line_text in enumerate(text.splitlines(), start=1):
        for rule_name, pattern in FORBIDDEN_TEXT_PATTERNS:
            if pattern.search(line_text):
                violations.append(
                    Violation(rel_path, line_number, "forbidden-pattern", rule_name)
                )
    return violations


def scan_repository(repo_root: Path) -> list[Violation]:
    """Scan a repository tree and return all guardrail violations."""
    violations: list[Violation] = []
    for file_path in _iter_python_files(repo_root):
        rel_path = file_path.relative_to(repo_root).as_posix()
        text = file_path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text, filename=rel_path)
        except SyntaxError as error:
            violations.append(
                Violation(rel_path, error.lineno or 0, "syntax", f"unparseable file: {error.msg}")
            )
            continue
        violations.extend(_check_imports(rel_path, tree))
        violations.extend(_check_text(rel_path, text))
    return violations


def main(argv: list[str]) -> int:
    repo_root = Path(argv[1]).resolve() if len(argv) > 1 else Path(__file__).resolve().parents[1]
    violations = scan_repository(repo_root)
    for violation in violations:
        print(violation)
    if violations:
        print(f"FAIL: {len(violations)} architecture guardrail violation(s).")
        return 1
    print("OK: architecture guardrails passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
