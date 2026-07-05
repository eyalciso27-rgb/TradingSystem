"""Negative tests for the forbidden text-pattern guardrails."""

from pathlib import Path
from types import ModuleType


def _scan_snippet(checker: ModuleType, tmp_path: Path, source: str) -> set[str]:
    file_path = tmp_path / "apps" / "api" / "src" / "trading_api" / "snippet.py"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(source, encoding="utf-8")
    return {violation.message for violation in checker.scan_repository(tmp_path)}


def test_hardcoded_paper_account_id_fails(checker: ModuleType, tmp_path: Path) -> None:
    messages = _scan_snippet(checker, tmp_path, 'ACCOUNT = "DU" + ""\nBAD = "DU1234567"\n')
    assert "hardcoded broker account id" in messages


def test_hardcoded_live_account_id_fails(checker: ModuleType, tmp_path: Path) -> None:
    messages = _scan_snippet(checker, tmp_path, 'BAD = "U7654321"\n')
    assert "hardcoded broker account id" in messages


def test_live_default_environment_fails(checker: ModuleType, tmp_path: Path) -> None:
    messages = _scan_snippet(checker, tmp_path, 'environment = "LIVE"\n')
    assert "LIVE used as a default environment" in messages


def test_queue_name_mixing_paper_and_live_fails(
    checker: ModuleType, tmp_path: Path
) -> None:
    messages = _scan_snippet(checker, tmp_path, 'QUEUE = "orders.paper-live.intent"\n')
    assert "queue/topic name mixing paper and live" in messages


def test_private_key_material_fails(checker: ModuleType, tmp_path: Path) -> None:
    marker = "-----BEGIN " + "PRIVATE KEY-----"
    messages = _scan_snippet(checker, tmp_path, f'KEY = "{marker}"\n')
    assert "private key material" in messages


def test_hardcoded_credential_fails(checker: ModuleType, tmp_path: Path) -> None:
    fake_token = "a" * 24
    messages = _scan_snippet(checker, tmp_path, f'api_key = "{fake_token}"\n')
    assert "hardcoded credential assignment" in messages


def test_hardcoded_windows_path_fails(checker: ModuleType, tmp_path: Path) -> None:
    messages = _scan_snippet(checker, tmp_path, 'ROOT = "D:\\\\project\\\\TradingSystem"\n')
    assert "hardcoded Windows path" in messages


def test_clean_file_passes(checker: ModuleType, tmp_path: Path) -> None:
    clean = (
        "from pathlib import Path\n"
        "\n"
        "PAPER_QUEUE = 'orders.paper.intent'\n"
        "root = Path('config') / 'policies'\n"
    )
    assert _scan_snippet(checker, tmp_path, clean) == set()
