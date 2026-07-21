"""IBKR ``EWrapper.error`` callback normalization (API 10.48 compatible).

**Blocking defect fixed here.** IBKR API 10.48 changed the error callback to

    error(reqId, errorTime, errorCode, errorString, advancedOrderRejectJson="")

inserting ``errorTime`` (epoch milliseconds) as the SECOND argument. Our
wrappers still declared the pre-10.48 shape
``error(reqId, code, message, advanced="")``, so a 10.48 call bound
``code = errorTime`` and ``message = errorCode`` — an ``int``. The sink then
executed ``message[:120]`` and raised
``TypeError("'int' object is not subscriptable")``, which the connect path
wrapped as ``PoiSessionError: connect failed: TypeError`` and thereby **masked
the real IBKR connection error**.

This module normalizes any supported callback shape into an explicit
``NormalizedBrokerError`` (a real ``int`` code and a real ``str`` message).

Disambiguation is by **arity plus argument type**, never by blind position, so
an integer can never be silently reinterpreted as a message:

- 10.48 shape — ``(reqId, errorTime, errorCode, errorString, [advanced])``:
  recognised when a 4th positional argument exists and the **third** argument
  is an integer (the error code).
- legacy shape — ``(reqId, errorCode, errorString, [advanced])``: recognised
  when the **second** argument is an integer and the **third** is text.
- Keyword calls are honoured directly (``errorCode``/``errorString`` etc.).

Anything else raises ``ErrorCallbackShapeError`` with a **sanitized**
diagnostic (arity, type names and keyword names only — never argument values),
so an unsupported shape fails closed with a clear message instead of an
unrelated ``TypeError``.

Read-only: this module exposes no order, cancel or submit surface, reads no
credentials and never logs account values.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeGuard

#: Error messages are truncated to this length before they reach any buffer.
MESSAGE_MAX_LENGTH = 120


class ErrorCallbackShapeError(Exception):
    """An IBKR error callback arrived in an unsupported/malformed shape."""


@dataclass(frozen=True, slots=True)
class NormalizedBrokerError:
    """A broker error normalized to a real int code + real str message."""

    code: int
    message: str
    request_id: int | None = None
    error_time_ms: int | None = None
    advanced_reject: str = ""


def _is_int(value: object) -> TypeGuard[int]:
    """True for a genuine integer (``bool`` is deliberately excluded)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_text(value: object) -> TypeGuard[str | bytes]:
    return isinstance(value, str | bytes)


def normalize_message(value: object) -> str:
    """Coerce any message payload to a bounded, printable string.

    Bytes are decoded leniently; non-string objects are rendered with
    ``str()``. The result is always a ``str`` and never longer than
    ``MESSAGE_MAX_LENGTH``.
    """
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    elif isinstance(value, str):
        text = value
    else:
        text = str(value)
    return text[:MESSAGE_MAX_LENGTH]


def _optional_int(value: object) -> int | None:
    return int(value) if _is_int(value) else None


def _describe(args: tuple[object, ...], kwargs: dict[str, object]) -> str:
    """Sanitized shape description: arity, type names and keyword names only."""
    types = ", ".join(type(arg).__name__ for arg in args) or "none"
    keys = ", ".join(sorted(kwargs)) or "none"
    return f"{len(args)} positional arg(s) [types: {types}]; keyword(s): {keys}"


def normalize_error_callback(
    *args: object, **kwargs: object
) -> NormalizedBrokerError:
    """Normalize an ``EWrapper.error`` invocation; fail closed if unsupported."""
    # -- keyword form (any API version) -----------------------------------
    code_kw = kwargs.get("errorCode", kwargs.get("error_code"))
    message_kw = kwargs.get("errorString", kwargs.get("error_string"))
    if code_kw is not None and message_kw is not None:
        if not _is_int(code_kw):
            raise ErrorCallbackShapeError(
                "IBKR error callback: errorCode is not an integer"
                f" (got {type(code_kw).__name__}); {_describe(args, kwargs)}"
            )
        return NormalizedBrokerError(
            code=code_kw,
            message=normalize_message(message_kw),
            request_id=_optional_int(kwargs.get("reqId", kwargs.get("req_id"))),
            error_time_ms=_optional_int(
                kwargs.get("errorTime", kwargs.get("error_time"))
            ),
            advanced_reject=normalize_message(
                kwargs.get(
                    "advancedOrderRejectJson",
                    kwargs.get("advanced_order_reject_json", ""),
                )
            ),
        )

    # -- 10.48 positional form: (reqId, errorTime, errorCode, errorString, …)
    # Recognised by a 4th argument WITH an integer error code in slot 3.
    if len(args) >= 4:
        modern_code, modern_message = args[2], args[3]
        if _is_int(modern_code) and _is_text(modern_message):
            return NormalizedBrokerError(
                code=modern_code,
                message=normalize_message(modern_message),
                request_id=_optional_int(args[0]),
                error_time_ms=_optional_int(args[1]),
                advanced_reject=normalize_message(args[4]) if len(args) >= 5 else "",
            )

    # -- legacy positional form: (reqId, errorCode, errorString, [advanced])
    if len(args) >= 3:
        legacy_code, legacy_message = args[1], args[2]
        if _is_int(legacy_code) and _is_text(legacy_message):
            return NormalizedBrokerError(
                code=legacy_code,
                message=normalize_message(legacy_message),
                request_id=_optional_int(args[0]),
                error_time_ms=None,
                advanced_reject=normalize_message(args[3]) if len(args) >= 4 else "",
            )

    raise ErrorCallbackShapeError(
        "unsupported IBKR error callback shape; refusing to guess "
        f"({_describe(args, kwargs)})"
    )


def ibapi_runtime_diagnostic() -> str:
    """A short, credential-free description of the installed IBKR API.

    Used only to make host-side version problems obvious in diagnostics; it
    imports ``ibapi`` lazily and never fails the caller.
    """
    try:
        import ibapi  # type: ignore[import-not-found]
    except ImportError:
        return (
            "ibapi: NOT INSTALLED (install the official IBKR Python API from the"
            " IBKR source distribution on the host — see the runbook)"
        )
    version = getattr(ibapi, "__version__", None)
    if version is None:
        raw = getattr(ibapi, "VERSION", None)
        version = str(raw) if raw is not None else "unknown"
    return f"ibapi: {version}"


__all__ = [
    "MESSAGE_MAX_LENGTH",
    "ErrorCallbackShapeError",
    "NormalizedBrokerError",
    "ibapi_runtime_diagnostic",
    "normalize_error_callback",
    "normalize_message",
]
