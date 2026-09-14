"""Shared fixtures for architecture guardrail tests (IMP-0002)."""

from types import ModuleType

import pytest

from scripts import check_architecture


@pytest.fixture(scope="session")
def checker() -> ModuleType:
    """Return the canonical scripts.check_architecture module."""
    return check_architecture
