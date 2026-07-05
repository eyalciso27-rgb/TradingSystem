"""Canonical environment and operating-mode naming policy (IMP-0002).

Sources of truth:

- Operating modes: MVP_Scope_v0.6 section 4 (RESEARCH, REPLAY, SHADOW,
  PAPER_MANUAL, PAPER_AUTO, LIVE_MANUAL, LIVE_LIMITED_AUTO).
- Environment axis: Data_Model_and_Retention_Specification_v0.6
  (environment text: RESEARCH/PAPER/LIVE).
- Paper/Live isolation: Security_and_Operations_Specification_v0.6 section 6.
- Enablement flags mirror docs/baseline-v0.5/BASELINE_MANIFEST.json
  (live_enabled=false, paper_auto_enabled=false).

This module is deliberately constants + deterministic validation only.
The versioned configuration loader arrives with IMP-0105. Since IMP-0101 the
canonical value sets are consumed from trading_contracts.enums (the single
source of truth); this module adds the enablement policy on top.
"""

import re
from collections.abc import Iterable
from typing import Final

from trading_contracts.enums import Environment, OperatingMode

CANONICAL_ENVIRONMENTS: Final[frozenset[str]] = frozenset(
    member.value for member in Environment
)

CANONICAL_OPERATING_MODES: Final[frozenset[str]] = frozenset(
    member.value for member in OperatingMode
)

LIVE_OPERATING_MODES: Final[frozenset[str]] = frozenset(
    {OperatingMode.LIVE_MANUAL.value, OperatingMode.LIVE_LIMITED_AUTO.value}
)

# Mirrors BASELINE_MANIFEST.json. Flipping these requires an approved ADR
# and an updated baseline; there is intentionally no runtime override.
LIVE_ENABLED: Final[bool] = False
PAPER_AUTO_ENABLED: Final[bool] = False

_MODE_TO_ENVIRONMENT: Final[dict[str, str]] = {
    "RESEARCH": "RESEARCH",
    "REPLAY": "RESEARCH",
    "SHADOW": "RESEARCH",
    "PAPER_MANUAL": "PAPER",
    "PAPER_AUTO": "PAPER",
    "LIVE_MANUAL": "LIVE",
    "LIVE_LIMITED_AUTO": "LIVE",
}

_ENVIRONMENT_TOKENS: Final[dict[str, str]] = {"PAPER": "paper", "LIVE": "live"}


class EnvironmentPolicyError(ValueError):
    """A value violates the canonical environment policy."""


class UnknownEnvironmentError(EnvironmentPolicyError):
    """The environment value is not one of the canonical environments."""


class UnknownOperatingModeError(EnvironmentPolicyError):
    """The operating mode is not one of the canonical operating modes."""


class ProhibitedOperatingModeError(EnvironmentPolicyError):
    """The operating mode is canonical but prohibited by current policy."""


def validate_environment(value: str) -> str:
    """Return the environment if canonical; fail explicitly otherwise.

    There is no default and no fallback: an unknown value never degrades
    to PAPER or LIVE.
    """
    if value not in CANONICAL_ENVIRONMENTS:
        raise UnknownEnvironmentError(
            f"Unknown environment {value!r}; canonical values are "
            f"{sorted(CANONICAL_ENVIRONMENTS)}. No fallback is applied."
        )
    return value


def validate_operating_mode(
    value: str,
    *,
    live_enabled: bool = LIVE_ENABLED,
    live_account_allowlist: frozenset[str] = frozenset(),
    paper_auto_enabled: bool = PAPER_AUTO_ENABLED,
) -> str:
    """Return the operating mode if canonical and permitted.

    LIVE modes require both explicit enablement and a non-empty account
    allowlist; both default to disabled per the baseline manifest.
    PAPER_AUTO requires explicit enablement. Unknown values always fail.
    """
    if value not in CANONICAL_OPERATING_MODES:
        raise UnknownOperatingModeError(
            f"Unknown operating mode {value!r}; canonical values are "
            f"{sorted(CANONICAL_OPERATING_MODES)}. No fallback is applied."
        )
    if value in LIVE_OPERATING_MODES:
        if not live_enabled or not live_account_allowlist:
            raise ProhibitedOperatingModeError(
                f"Operating mode {value!r} is prohibited: live trading requires "
                "explicit enablement and a non-empty live account allowlist "
                "(baseline: live_enabled=false)."
            )
    if value == "PAPER_AUTO" and not paper_auto_enabled:
        raise ProhibitedOperatingModeError(
            "Operating mode 'PAPER_AUTO' is prohibited: baseline sets "
            "paper_auto_enabled=false."
        )
    return value


def environment_for_mode(mode: str) -> str:
    """Map a canonical operating mode to its environment axis value."""
    if mode not in CANONICAL_OPERATING_MODES:
        raise UnknownOperatingModeError(
            f"Unknown operating mode {mode!r}; canonical values are "
            f"{sorted(CANONICAL_OPERATING_MODES)}."
        )
    return _MODE_TO_ENVIRONMENT[mode]


def _contains_token(name: str, token: str) -> bool:
    return re.search(rf"(?<![a-z]){token}(?![a-z])", name.lower()) is not None


def validate_environment_scoped_name(name: str, environment: str) -> str:
    """Validate a queue/topic/config-path name against Paper/Live separation.

    Per Security and Operations Specification section 6, Paper and Live never
    share queues or naming. A PAPER-scoped name must contain the ``paper``
    token and never ``live``; a LIVE-scoped name the inverse. RESEARCH-scoped
    names must contain neither token.
    """
    env = validate_environment(environment)
    own_token = _ENVIRONMENT_TOKENS.get(env)
    for other_env, other_token in _ENVIRONMENT_TOKENS.items():
        if other_env != env and _contains_token(name, other_token):
            raise EnvironmentPolicyError(
                f"Name {name!r} is scoped to {env} but contains the "
                f"{other_env} token {other_token!r}."
            )
    if own_token is not None and not _contains_token(name, own_token):
        raise EnvironmentPolicyError(
            f"Name {name!r} is scoped to {env} but does not contain the "
            f"required token {own_token!r}."
        )
    return name


def validate_account_allowlists(
    paper_accounts: Iterable[str], live_accounts: Iterable[str]
) -> None:
    """Fail if any account identifier appears in both allowlists.

    Paper and Live must not share account identifiers (Security and
    Operations Specification section 6).
    """
    shared = frozenset(paper_accounts) & frozenset(live_accounts)
    if shared:
        raise EnvironmentPolicyError(
            f"Account identifiers present in both Paper and Live allowlists: "
            f"{sorted(shared)}."
        )
