"""Shared fixtures for canonical contract tests (IMP-0101)."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def generator() -> ModuleType:
    """Load scripts/generate_contracts.py as a module."""
    script_path = REPO_ROOT / "scripts" / "generate_contracts.py"
    spec = importlib.util.spec_from_file_location("generate_contracts", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["generate_contracts"] = module
    spec.loader.exec_module(module)
    return module
