"""Positive and negative tests for the import-boundary guardrails."""

from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]


def _write_module(root: Path, rel_path: str, source: str) -> None:
    file_path = root / Path(rel_path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(source, encoding="utf-8")


def _rules(checker: ModuleType, root: Path) -> set[str]:
    return {violation.rule for violation in checker.scan_repository(root)}


def test_legal_imports_pass(checker: ModuleType, tmp_path: Path) -> None:
    _write_module(
        tmp_path,
        "packages/py/domain/src/trading_domain/logic.py",
        "import decimal\nimport trading_contracts\n",
    )
    _write_module(
        tmp_path,
        "apps/api/src/trading_api/app.py",
        "import fastapi\nimport trading_application\n",
    )
    assert checker.scan_repository(tmp_path) == []


def test_ibapi_inside_broker_ibkr_is_allowed(checker: ModuleType, tmp_path: Path) -> None:
    _write_module(
        tmp_path,
        "packages/py/broker-ibkr/src/trading_broker_ibkr/client.py",
        "import ibapi\nfrom ibapi.client import EClient\n",
    )
    assert checker.scan_repository(tmp_path) == []


def test_ibapi_outside_broker_ibkr_fails(checker: ModuleType, tmp_path: Path) -> None:
    _write_module(
        tmp_path,
        "packages/py/domain/src/trading_domain/bad.py",
        "import ibapi\n",
    )
    assert "sdk-boundary" in _rules(checker, tmp_path)


def test_from_import_of_ibapi_is_also_caught(checker: ModuleType, tmp_path: Path) -> None:
    _write_module(
        tmp_path,
        "apps/core-worker/src/trading_core_worker/bad.py",
        "from ibapi.client import EClient\n",
    )
    assert "sdk-boundary" in _rules(checker, tmp_path)


def test_fastapi_outside_api_app_fails(checker: ModuleType, tmp_path: Path) -> None:
    _write_module(
        tmp_path,
        "packages/py/application/src/trading_application/bad.py",
        "import fastapi\n",
    )
    assert "sdk-boundary" in _rules(checker, tmp_path)


def test_playwright_is_forbidden_in_python(checker: ModuleType, tmp_path: Path) -> None:
    _write_module(
        tmp_path,
        "apps/scheduler/src/trading_scheduler/bad.py",
        "from playwright.sync_api import sync_playwright\n",
    )
    assert "sdk-boundary" in _rules(checker, tmp_path)


def test_domain_may_not_import_infrastructure(checker: ModuleType, tmp_path: Path) -> None:
    _write_module(
        tmp_path,
        "packages/py/domain/src/trading_domain/bad.py",
        "import trading_persistence\n",
    )
    assert "layer-boundary" in _rules(checker, tmp_path)


def test_contracts_may_not_import_internal_packages(
    checker: ModuleType, tmp_path: Path
) -> None:
    _write_module(
        tmp_path,
        "packages/py/contracts/src/trading_contracts/bad.py",
        "import trading_domain\n",
    )
    assert "layer-boundary" in _rules(checker, tmp_path)


def test_domain_may_not_use_http_clients(checker: ModuleType, tmp_path: Path) -> None:
    _write_module(
        tmp_path,
        "packages/py/domain/src/trading_domain/bad.py",
        "import httpx\n",
    )
    assert "domain-purity" in _rules(checker, tmp_path)


def test_application_may_not_import_broker_adapter(
    checker: ModuleType, tmp_path: Path
) -> None:
    _write_module(
        tmp_path,
        "packages/py/application/src/trading_application/bad.py",
        "from trading_broker_ibkr import anything\n",
    )
    assert "broker-adapter-boundary" in _rules(checker, tmp_path)


def test_execution_agent_may_import_broker_adapter(
    checker: ModuleType, tmp_path: Path
) -> None:
    _write_module(
        tmp_path,
        "apps/execution-agent/src/trading_execution_agent/runtime.py",
        "import trading_broker_ibkr\n",
    )
    assert checker.scan_repository(tmp_path) == []


def test_real_repository_is_compliant(checker: ModuleType) -> None:
    violations = checker.scan_repository(REPO_ROOT)
    assert violations == [], "\n".join(str(violation) for violation in violations)
