"""Central secret redaction applied to every emitted log record.

Services never implement their own scrubbing: the structured formatter runs
each complete record through :func:`redact` before serialisation, so
secrets, tokens, passwords, cookies, sessions and credentials can never
reach operational logs regardless of which service emitted the record.
"""

from __future__ import annotations

import re
from typing import Final

REDACTED: Final[str] = "[REDACTED]"

# A key is sensitive when any marker appears in its lowercased name.
SENSITIVE_KEY_MARKERS: Final[tuple[str, ...]] = (
    "secret",
    "token",
    "password",
    "passwd",
    "cookie",
    "session",
    "credential",
    "authorization",
    "api_key",
    "apikey",
    "private_key",
    "dsn",
)

# Value patterns scrubbed even under non-sensitive keys.
_BEARER_RE: Final[re.Pattern[str]] = re.compile(r"(?i)\bbearer\s+\S+")
_URL_CREDENTIALS_RE: Final[re.Pattern[str]] = re.compile(r"(?i)\b(\w+?://[^/\s:@]+):[^@\s]+@")


def is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in SENSITIVE_KEY_MARKERS)


def _redact_text(value: str) -> str:
    value = _BEARER_RE.sub(REDACTED, value)
    return _URL_CREDENTIALS_RE.sub(rf"\1:{REDACTED}@", value)


def redact(value: object) -> object:
    """Return a deep copy of ``value`` with sensitive content removed.

    Mappings are scrubbed by key name; strings are scrubbed for embedded
    bearer tokens and URL userinfo passwords; sequences recurse element-wise.
    """
    if isinstance(value, dict):
        return {
            key: REDACTED if is_sensitive_key(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value
