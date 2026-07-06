"""SQLAlchemy declarative base, naming convention and canonical types (IMP-0201).

The MVP database is PostgreSQL (Technology_and_Deployment_Decisions_v0.6
ADR-006). Conventions follow Data_Model_and_Retention_Specification_v0.6
section 4:

- tables are snake_case plural; primary key is ``id uuid`` generated in the
  application layer (never a database default);
- ``environment`` and other enum-like values are stored as ``text`` and
  constrained with CHECK against the canonical enum values (no native PG ENUM,
  matching the spec's ``text`` columns);
- money/price use NUMERIC(20,8), quantities NUMERIC(28,10) — never FLOAT;
  currency is CHAR(3) ISO-4217; every timestamp is TIMESTAMP WITH TIME ZONE.

A deterministic naming convention keeps constraint/index names stable across
environments so migrations and downgrades are reproducible.
"""

from collections.abc import Iterable
from enum import StrEnum

from sqlalchemy import CheckConstraint, MetaData, Numeric
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# Canonical NUMERIC types (Data_Model section 7). Money/price never use FLOAT.
Money = Numeric(20, 8)
Price = Numeric(20, 8)
Quantity = Numeric(28, 10)
ScoreNumeric = Numeric(18, 10)


class Base(DeclarativeBase):
    """Declarative base carrying the shared naming convention."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def enum_check(column: str, enum_class: type[StrEnum], name: str) -> CheckConstraint:
    """A CHECK constraint pinning ``column`` to a canonical enum's values."""
    allowed = ", ".join(f"'{member.value}'" for member in enum_class)
    return CheckConstraint(f"{column} IN ({allowed})", name=name)


def values_check(column: str, values: Iterable[str], name: str) -> CheckConstraint:
    """A CHECK constraint pinning ``column`` to an explicit value list."""
    allowed = ", ".join(f"'{value}'" for value in values)
    return CheckConstraint(f"{column} IN ({allowed})", name=name)


def raw_check(expression: str, name: str) -> CheckConstraint:
    """A CHECK constraint from a raw boolean SQL expression."""
    return CheckConstraint(expression, name=name)
