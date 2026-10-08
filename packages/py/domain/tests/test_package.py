"""Tests for the public surface of :mod:`trading_domain`."""

import trading_domain


def test_package_can_be_imported() -> None:
    assert trading_domain.__name__ == "trading_domain"


def test_package_has_no_public_exports_while_it_is_a_skeleton() -> None:
    assert trading_domain.__all__ == []
