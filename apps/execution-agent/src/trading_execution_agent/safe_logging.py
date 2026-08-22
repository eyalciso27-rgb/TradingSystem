"""Keep third-party broker chatter out of owner-visible output.

The probe promises the paper account id is *read, never written, never printed*.
The first real owner-host run showed that promise was false, and not because of
anything this repository rendered: at INFO level the IBKR SDK's own
``ibapi.utils`` logger echoed the raw ``managedAccounts`` payload, DU number and
all. Redacting our own ``render()`` output could never have caught that, because
our code was not the thing printing it.

Two independent defences, because either alone is a single point of failure:

1. the noisy SDK protocol loggers are silenced for the probe unless the owner
   explicitly asks for them, so the payloads are not emitted at all;
2. a redaction filter is installed on the root logger, so anything that *does*
   reach a handler -- from the SDK, from a dependency, from a future code path
   nobody audited -- has account identifiers, DSNs and tokens removed first.

The second one is the load-bearing one. Suppression depends on knowing every
logger name in advance, and that is exactly the assumption the first run broke.
"""

from __future__ import annotations

import logging
import re

#: Broker SDK loggers that emit raw protocol payloads at INFO. ``ibapi.utils``
#: is the one that printed the managed-account list; the others are silenced
#: with it because they carry the same class of traffic.
NOISY_BROKER_LOGGERS: tuple[str, ...] = (
    "ibapi",
    "ibapi.utils",
    "ibapi.client",
    "ibapi.wrapper",
    "ibapi.connection",
    "ibapi.reader",
    "ibapi.decoder",
)

#: What must never reach owner-visible output, whoever emitted it.
#:
#: Account ids first: IBKR paper accounts are ``DU`` + digits and live accounts
#: ``U`` + digits, and the managed-account payload is a comma-separated list of
#: them. The rest are the usual secret shapes a stack trace or a driver message
#: can carry.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Broker account identifiers, anywhere in the line.
    (re.compile(r"\b(?:DU|DF|U)\d{5,}\b"), "[account]"),
    # A DSN with credentials: postgresql://user:password@host/db
    (re.compile(r"\b[a-zA-Z][\w+.-]*://[^\s/@]+:[^\s/@]+@[^\s]*"), "[dsn]"),
    # Any other URI, which may itself carry a token in the query string.
    (re.compile(r"\b[a-zA-Z][\w+.-]*://[^\s]+"), "[uri]"),
    # Explicit key=value secrets. The key may be prefixed or suffixed
    # (POSTGRES_PASSWORD, api_key_v2), so no plain word boundary in front of
    # it, and the VALUE consumes the rest of the line: "authorization: Bearer
    # <token>" leaves the token behind if only the first word is taken.
    (
        re.compile(
            r"([\w-]*(?:password|passwd|secret|token|api[_-]?key|authorization)"
            r"[\w-]*)\s*[=:]\s*\S.*",
            re.IGNORECASE,
        ),
        r"\1=[redacted]",
    ),
)


def redact(text: str) -> str:
    """Remove account identifiers and secret shapes from one line of text."""
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class SecretRedactingFilter(logging.Filter):
    """Redacts every record that passes through, including third-party ones.

    Applied to the record's message and arguments rather than to the formatted
    output, so it works regardless of which formatter or handler is installed --
    a filter on the handler would be bypassed by a second handler nobody
    remembered to configure.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    key: redact(value) if isinstance(value, str) else value
                    for key, value in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    redact(value) if isinstance(value, str) else value
                    for value in record.args
                )
        return True


def silence_broker_protocol_logging(
    *, debug: bool = False, level: int = logging.WARNING
) -> None:
    """Stop the broker SDK echoing protocol payloads into owner output.

    ``debug=True`` leaves the SDK loggers alone for deliberate troubleshooting;
    the redaction filter still applies, so even then the account id does not
    survive to the handler.
    """
    if debug:
        return
    for name in NOISY_BROKER_LOGGERS:
        logging.getLogger(name).setLevel(level)


def install_owner_output_protection(*, debug: bool = False) -> None:
    """Both defences, in the order they take effect."""
    silence_broker_protocol_logging(debug=debug)
    root = logging.getLogger()
    if not any(isinstance(existing, SecretRedactingFilter) for existing in root.filters):
        root.addFilter(SecretRedactingFilter())
    # A filter on the ROOT LOGGER is not applied to records that propagate up
    # from child loggers -- only records logged directly on root pass through
    # it. So the handlers get it too, which is what actually covers ibapi.
    for handler in root.handlers:
        if not any(
            isinstance(existing, SecretRedactingFilter) for existing in handler.filters
        ):
            handler.addFilter(SecretRedactingFilter())


__all__ = [
    "NOISY_BROKER_LOGGERS",
    "SecretRedactingFilter",
    "install_owner_output_protection",
    "redact",
    "silence_broker_protocol_logging",
]
