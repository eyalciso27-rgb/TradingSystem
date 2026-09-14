"""Repository scripts have one canonical package identity."""

from __future__ import annotations

import importlib
import sys
import tomllib
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]


def test_architecture_checker_fixture_uses_canonical_package(
    checker: ModuleType,
) -> None:
    canonical = importlib.import_module("scripts.check_architecture")

    assert checker is canonical
    assert checker.__name__ == "scripts.check_architecture"
    assert "check_architecture" not in sys.modules


def test_systemd_contract_has_only_its_canonical_package_identity() -> None:
    canonical = importlib.import_module("scripts.systemd_contract")

    assert canonical.__name__ == "scripts.systemd_contract"
    assert "systemd_contract" not in sys.modules


def test_scripts_directory_is_not_a_competing_mypy_package_base() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    mypy_path = config["tool"]["mypy"]["mypy_path"].split(":")

    assert "scripts" not in mypy_path
    assert (ROOT / "scripts" / "__init__.py").is_file()
