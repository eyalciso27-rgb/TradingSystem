"""Smoke tests for the IMP-0001 monorepo skeleton.

Verifies that every workspace package is installed and importable.
No business logic is exercised because none exists yet.
"""

import importlib

import pytest

WORKSPACE_MODULES = [
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
]


@pytest.mark.parametrize("module_name", WORKSPACE_MODULES)
def test_workspace_module_imports(module_name: str) -> None:
    module = importlib.import_module(module_name)
    assert module.__name__ == module_name
