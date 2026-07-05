"""Shared fixtures for architecture guardrail tests (IMP-0002)."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def checker() -> ModuleType:
    """Load scripts/check_architecture.py as a module."""
    script_path = REPO_ROOT / "scripts" / "check_architecture.py"
    spec = importlib.util.spec_from_file_location("check_architecture", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_architecture"] = module
    spec.loader.exec_module(module)
    return module
