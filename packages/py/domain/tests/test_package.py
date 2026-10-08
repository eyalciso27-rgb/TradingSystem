"""Tests for the public surface of the trading domain package."""

from importlib import import_module
from importlib.metadata import PackageNotFoundError, metadata
from pathlib import Path

import trading_domain


def test_domain_module_is_importable_by_name() -> None:
    """The installed package exposes its domain module."""
    assert import_module("trading_domain") is trading_domain


def test_domain_module_has_no_public_exports_yet() -> None:
    """The skeleton package deliberately does not publish business objects."""
    assert trading_domain.__all__ == []


def test_domain_package_includes_type_information_marker() -> None:
    """Static type checkers can identify the package as typed."""
    marker = Path(trading_domain.__file__).with_name("py.typed")

    assert marker.is_file()


def test_installed_distribution_metadata_matches_domain_package() -> None:
    """Workspace installation preserves the package's declared identity."""
    try:
        distribution = metadata("trading-domain")
    except PackageNotFoundError:
        # A bare pytest invocation imports from the repository's configured
        # pythonpath and does not necessarily install workspace distributions.
        return

    assert distribution["Name"] == "trading-domain"
    assert distribution["Version"] == "0.1.0"

