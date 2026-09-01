"""Single-instance ownership for the runtime (A2).

Thin factory over the persistence ``AdvisoryLock``. The lock itself is the
primitive; this names the runtime's protected resource and gives the service one
place to build it. The lock satisfies the ``OwnershipGuard`` port structurally,
so the service depends on the port, not on this module.
"""

from __future__ import annotations

from trading_persistence.advisory_lock import PAPER_RUNTIME_LOCK_NAME, AdvisoryLock


def build_paper_runtime_ownership(engine: object) -> AdvisoryLock:
    """The single-instance ownership lock for the autonomous Paper runtime.

    ``engine`` is an opaque SQLAlchemy ``Engine`` -- the app never names the
    driver type (that symbol lives only under the persistence package), so it is
    passed through untyped and the persistence ``AdvisoryLock`` gives it a type.
    """
    return AdvisoryLock(engine, name=PAPER_RUNTIME_LOCK_NAME)  # type: ignore[arg-type]


__all__ = ["build_paper_runtime_ownership"]
