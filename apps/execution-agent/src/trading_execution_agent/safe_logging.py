"""Keep third-party broker chatter out of owner-visible output.

The probe promises the paper account id is *read, never written, never printed*.
The first real owner-host run showed that promise was false, and not because of
anything this repository rendered: at INFO level the IBKR SDK's own
``ibapi.utils`` logger echoed the raw ``managedAccounts`` payload, DU number and
all. Redacting our own ``render()`` output could never have caught that, because
our code was not the thing printing it.

Three independent defences, because any one alone is a single point of failure:

1. the noisy SDK protocol loggers are silenced for the probe unless the owner
   explicitly asks for them, so the payloads are not emitted at all;
2. the EXACT account id, once resolved from the secret store, is registered as a
   sensitive literal and replaced wherever it appears, in any format;
3. pattern redaction removes account-shaped identifiers, DSNs and key/value
   secrets generally, for values we do not hold.

Defence 2 is the one that actually protects the secret we possess. Pattern
matching cannot be load-bearing for it: the first owner-host run showed a real
IBKR account identifier whose shape the original pattern did not match, and a
regex written today cannot anticipate the account formats IBKR introduces
tomorrow. Defence 3 remains, for account ids we have never seen.

Suppression (defence 1) is the weakest, because it depends on knowing every
logger name in advance -- which is exactly the assumption the first run broke.
"""

from __future__ import annotations

import logging
import re
import threading

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

#: Exact secret values, registered once resolved. Checked BEFORE the patterns,
#: because an exact literal cannot be wrong about its own shape and a pattern
#: can. Guarded by a lock: the SDK logs from its own reader thread.
_SENSITIVE_VALUES: set[str] = set()
_SENSITIVE_LOCK = threading.Lock()

#: The shortest value worth registering. Redacting a one- or two-character
#: string would mangle every message that happens to contain those letters.
MIN_SENSITIVE_LENGTH = 4


def register_sensitive_value(value: str | None) -> None:
    """Register one exact value to be removed from all owner-visible output.

    Used for the resolved paper account id. Whatever format IBKR uses now or
    later -- digits, letters, punctuation -- this removes that exact string,
    which is a guarantee no pattern can make.

    The value itself is never logged by this function, and the registry is
    never exposed.
    """
    if not value:
        return
    candidate = value.strip()
    if len(candidate) < MIN_SENSITIVE_LENGTH:
        return
    with _SENSITIVE_LOCK:
        _SENSITIVE_VALUES.add(candidate)


def clear_sensitive_values() -> None:
    """Forget every registered literal. For tests and process teardown."""
    with _SENSITIVE_LOCK:
        _SENSITIVE_VALUES.clear()


#: What must never reach owner-visible output, whoever emitted it.
#:
#: Account ids first. The original pattern was ``(?:DU|DF|U)\d{5,}`` -- digits
#: only -- and the first real owner-host run produced an identifier with an
#: ALPHABETIC character after the prefix, which that pattern did not match. It
#: now accepts alphanumerics, and the exact resolved value is registered as a
#: literal above so recognition is not the only thing standing between the
#: account id and the owner's terminal.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Broker account identifiers, anywhere in the line: a DU/DF/U prefix
    # followed by at least five alphanumerics, with at least one digit so
    # ordinary words are not swallowed.
    (
        re.compile(r"\b(?:DU|DF|U)(?=[A-Za-z0-9]{5,}\b)(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{5,}\b"),
        "[account]",
    ),
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
    """Remove registered secrets and secret shapes from one line of text."""
    with _SENSITIVE_LOCK:
        # Longest first, so a value that contains another is replaced whole.
        registered = sorted(_SENSITIVE_VALUES, key=len, reverse=True)
    for secret in registered:
        text = text.replace(secret, "[redacted]")
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def redact_object(value: object) -> object:
    """Redact a logging argument of any shape, one level deep into containers.

    IBKR logs dicts and tuples of protocol fields, so a filter that only looked
    at ``str`` arguments would let ``{'accountsList': 'DU...'}`` through.
    """
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: redact_object(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        rebuilt = [redact_object(item) for item in value]
        if isinstance(value, tuple):
            return tuple(rebuilt)
        if isinstance(value, set):
            return set(rebuilt)
        return rebuilt
    # Anything else is rendered by the formatter through repr()/str(); redact
    # that rendering rather than trusting an object we do not control.
    text = str(value)
    cleaned = redact(text)
    return cleaned if cleaned != text else value


class SecretRedactingFilter(logging.Filter):
    """Redacts every record that passes through, including third-party ones.

    Applied to the record's message and arguments rather than to the formatted
    output, so it works regardless of which formatter or handler is installed --
    a filter on the handler would be bypassed by a second handler nobody
    remembered to configure.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_object(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    key: redact_object(value) for key, value in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(redact_object(value) for value in record.args)
        # An exception attached to the record is rendered by the formatter from
        # the traceback, which this filter never sees. The probe does not print
        # tracebacks in normal mode for exactly that reason.
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


def safe_failure(code: str, error: BaseException) -> str:
    """A stable owner-visible failure line that cannot carry a secret.

    The exception TYPE and a fixed code, never ``str(error)``. Dependency
    exception text is written by someone else and routinely contains a DSN, a
    password, a filesystem path or an SDK payload -- and it reaches the terminal
    through ``print``, which no logging filter can see.
    """
    return f"{code}: {type(error).__name__}"


__all__ = [
    "MIN_SENSITIVE_LENGTH",
    "NOISY_BROKER_LOGGERS",
    "SecretRedactingFilter",
    "clear_sensitive_values",
    "install_owner_output_protection",
    "redact",
    "redact_object",
    "register_sensitive_value",
    "safe_failure",
    "silence_broker_protocol_logging",
]
