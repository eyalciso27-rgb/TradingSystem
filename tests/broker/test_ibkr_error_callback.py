"""IBKR error-callback compatibility drills (no IBKR contact).

Regression coverage for the blocking defect found during POI-MD-001 evidence
collection: IBKR API 10.48 inserts ``errorTime`` as the second argument of
``EWrapper.error``, so the pre-10.48 wrapper signature bound an ``int`` to
``message`` and ``message[:120]`` raised
``TypeError("'int' object is not subscriptable")`` — masking the real IBKR
connection error behind ``PoiSessionError: connect failed: TypeError``.
"""

from __future__ import annotations

from typing import Any

import pytest
from trading_broker_ibkr.ibkr_error_callback import (
    MESSAGE_MAX_LENGTH,
    ErrorCallbackShapeError,
    ibapi_runtime_diagnostic,
    normalize_error_callback,
    normalize_message,
)

# The exact 10.48 call site that triggered the defect:
#   self.wrapper.error(NO_VALID_ID, currentTimeMillis(),
#                      CONNECT_FAIL.code(), CONNECT_FAIL.msg())
NO_VALID_ID = -1
ERROR_TIME_MS = 1_752_600_000_000
CONNECT_FAIL_CODE = 502
CONNECT_FAIL_MSG = "Couldn't connect to TWS. Confirm that API is enabled."


# -- 10.48 shape -------------------------------------------------------------


def test_ibkr_1048_four_argument_callback_is_mapped_correctly() -> None:
    normalized = normalize_error_callback(
        NO_VALID_ID, ERROR_TIME_MS, CONNECT_FAIL_CODE, CONNECT_FAIL_MSG
    )
    assert normalized.code == CONNECT_FAIL_CODE  # NOT errorTime
    assert normalized.message == CONNECT_FAIL_MSG  # NOT the int code
    assert normalized.request_id == NO_VALID_ID
    assert normalized.error_time_ms == ERROR_TIME_MS
    assert isinstance(normalized.code, int)
    assert isinstance(normalized.message, str)


def test_ibkr_1048_five_argument_callback_with_advanced_reject() -> None:
    normalized = normalize_error_callback(
        7, ERROR_TIME_MS, 201, "Order rejected", '{"reason":"x"}'
    )
    assert (normalized.code, normalized.message) == (201, "Order rejected")
    assert normalized.advanced_reject == '{"reason":"x"}'


def test_error_time_is_never_mistaken_for_the_code() -> None:
    normalized = normalize_error_callback(-1, ERROR_TIME_MS, 2104, "Market data farm OK")
    assert normalized.code == 2104
    assert normalized.code != ERROR_TIME_MS


# -- legacy shape (still supported) ------------------------------------------


def test_legacy_three_argument_callback_still_supported() -> None:
    normalized = normalize_error_callback(-1, 502, CONNECT_FAIL_MSG)
    assert (normalized.code, normalized.message) == (502, CONNECT_FAIL_MSG)
    assert normalized.error_time_ms is None


def test_legacy_four_argument_callback_with_advanced_reject() -> None:
    normalized = normalize_error_callback(3, 201, "Rejected", '{"a":1}')
    assert (normalized.code, normalized.message) == (201, "Rejected")
    assert normalized.advanced_reject == '{"a":1}'
    assert normalized.error_time_ms is None  # legacy carries no error time


def test_legacy_and_1048_four_arg_forms_are_disambiguated_by_type() -> None:
    # 4 args where slot 2 is int  -> 10.48 (errorTime, errorCode, errorString)
    modern = normalize_error_callback(-1, ERROR_TIME_MS, 502, "boom")
    # 4 args where slot 2 is text -> legacy (errorCode, errorString, advanced)
    legacy = normalize_error_callback(-1, 502, "boom", "")
    assert modern.code == legacy.code == 502
    assert modern.message == legacy.message == "boom"


# -- keyword form -------------------------------------------------------------


def test_keyword_callback_is_honoured() -> None:
    normalized = normalize_error_callback(
        reqId=9, errorTime=ERROR_TIME_MS, errorCode=354, errorString="not subscribed"
    )
    assert (normalized.code, normalized.message) == (354, "not subscribed")
    assert normalized.request_id == 9


def test_keyword_callback_with_non_integer_code_fails_closed() -> None:
    with pytest.raises(ErrorCallbackShapeError, match="not an integer"):
        normalize_error_callback(errorCode="502", errorString="boom")


# -- malformed / unsupported shapes fail closed (never a TypeError) ----------


@pytest.mark.parametrize(
    "args",
    [
        (),                       # no arguments at all
        (-1,),                    # only a request id
        (-1, "boom"),             # too few, wrong types
        (-1, "a", "b"),           # code is not an integer
        (None, None, None, None),  # all wrong types
    ],
)
def test_malformed_callback_raises_shape_error_not_typeerror(args: tuple[Any, ...]) -> None:
    with pytest.raises(ErrorCallbackShapeError) as excinfo:
        normalize_error_callback(*args)
    detail = str(excinfo.value)
    assert "unsupported IBKR error callback shape" in detail
    assert "positional arg(s)" in detail  # arity reported


def test_shape_error_diagnostic_is_sanitized() -> None:
    with pytest.raises(ErrorCallbackShapeError) as excinfo:
        normalize_error_callback(-1, "SECRET-VALUE", "DU1234567")
    detail = str(excinfo.value)
    # type names only — never the argument values
    assert "SECRET-VALUE" not in detail
    assert "DU1234567" not in detail
    assert "str" in detail


def test_bool_is_not_accepted_as_an_error_code() -> None:
    with pytest.raises(ErrorCallbackShapeError):
        normalize_error_callback(-1, True, "boom")


# -- message normalization ----------------------------------------------------


def test_message_normalization_of_non_string_payloads() -> None:
    assert normalize_message(b"bytes message") == "bytes message"
    assert normalize_message(502) == "502"
    assert normalize_message(None) == "None"
    assert normalize_message(b"\xff\xfe") != ""  # lenient decode, never raises


def test_message_is_truncated() -> None:
    assert len(normalize_message("x" * 500)) == MESSAGE_MAX_LENGTH


def test_bytes_error_string_from_callback_becomes_str() -> None:
    normalized = normalize_error_callback(-1, ERROR_TIME_MS, 502, b"binary boom")
    assert normalized.message == "binary boom"
    assert isinstance(normalized.message, str)


# -- runtime diagnostic -------------------------------------------------------


def test_runtime_diagnostic_reports_missing_ibapi(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *rest: Any, **kw: Any) -> Any:
        if name == "ibapi":
            raise ImportError("no ibapi")
        return real_import(name, *rest, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    diagnostic = ibapi_runtime_diagnostic()
    assert "NOT INSTALLED" in diagnostic
    assert "source distribution" in diagnostic


def test_runtime_diagnostic_reports_version(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types

    module = types.ModuleType("ibapi")
    module.__version__ = "10.48.1"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ibapi", module)
    assert ibapi_runtime_diagnostic() == "ibapi: 10.48.1"


# -- no order surface ---------------------------------------------------------


def test_module_exposes_no_order_surface() -> None:
    import trading_broker_ibkr.ibkr_error_callback as module

    assert module.__file__ is not None
    with open(module.__file__, encoding="utf-8") as handle:
        source = handle.read()
    for token in (
        "place" + "Order", "cancel" + "Order", "reqGlobal" + "Cancel",
        "OrderIntent", "SubmitAuthorization", "getpass", "os.environ",
    ):
        assert token not in source, token
