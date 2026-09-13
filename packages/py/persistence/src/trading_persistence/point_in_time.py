"""Point-in-time (as-of) query helpers (IMP-0202).

Authority: Data_Model_and_Retention_Specification_v0.6 section 24.2
(observed/ingested/effective/available/superseded, as_of) and
Decision_Contract_v0.8 section 12.7: replay must never use a record whose
``available_at`` is later than ``as_of``.

A row is visible at ``as_of`` iff::

    available_at <= as_of AND (superseded_at IS NULL OR superseded_at > as_of)

There is no fallback: a record only becomes visible once it was genuinely
available to the system, and a superseded record stops being visible from its
supersession moment. ``as_of`` must be timezone-aware (UTC storage).
"""

from datetime import datetime
from typing import TypeVar, cast

from sqlalchemy import ColumnElement, Select, and_, or_, select
from sqlalchemy.orm import DeclarativeBase, InstrumentedAttribute


class NaiveAsOfError(ValueError):
    """The as_of cut-off must be timezone-aware."""


def _require_aware(as_of: datetime) -> datetime:
    if as_of.tzinfo is None:
        raise NaiveAsOfError("as_of must be timezone-aware (UTC storage)")
    return as_of


def as_of_clause(model: type[DeclarativeBase], as_of: datetime) -> ColumnElement[bool]:
    """Visibility predicate for one point-in-time model at ``as_of``.

    The model must carry ``available_at`` (non-null) and ``superseded_at``
    (nullable) columns per Data_Model section 24.2.
    """
    _require_aware(as_of)
    available_at = cast(
        "InstrumentedAttribute[datetime]", getattr(model, "available_at")  # noqa: B009
    )
    superseded_at = cast(
        "InstrumentedAttribute[datetime | None]", getattr(model, "superseded_at")  # noqa: B009
    )
    return and_(
        available_at <= as_of,
        or_(superseded_at.is_(None), superseded_at > as_of),
    )


_M = TypeVar("_M", bound=DeclarativeBase)


def select_as_of(  # noqa: UP047 - architecture check runs on pre-3.12 Python
    model: type[_M], as_of: datetime
) -> Select[tuple[_M]]:
    """SELECT rows of ``model`` visible at ``as_of`` (no future records)."""
    return select(model).where(as_of_clause(model, as_of))
