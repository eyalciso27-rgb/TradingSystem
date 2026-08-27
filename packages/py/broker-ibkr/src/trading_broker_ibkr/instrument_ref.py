"""The opaque IBKR security reference: minted and read ONLY in this package.

``BrokerInstrument.broker_instrument_ref`` is opaque to everything outside
``trading_broker_ibkr``. That is the whole contract: application code compares
references for equality and never parses them, because the moment it does, IBKR's
identity scheme becomes part of the domain.

So the translation lives here, once, and both the order path and the market-data
path use it. It was previously defined inside ``paper_adapter``, which meant the
market-data client could not reach it without importing the order adapter -- and
that is precisely how the quote leg ended up still requesting by ticker while
everything around it had moved to the resolved security.
"""

from __future__ import annotations

#: The scheme for an IBKR security reference. Only this package may read it.
INSTRUMENT_REF_PREFIX = "conid:"


def encode_instrument_ref(con_id: object) -> str:
    """Mint the opaque reference for an IBKR-resolved security.

    Fails closed on anything that is not a real, positive contract id. The
    resolver used to write ``f"conid:{getattr(resolved, 'conId', 0)}"``, which
    produced ``"conid:0"`` whenever IBKR reported no usable id -- a truthy
    string that is not a security identity, and which every downstream check
    would then have compared happily against itself.
    """
    try:
        numeric = int(str(con_id).strip())
    except (TypeError, ValueError):
        return ""
    if numeric <= 0:
        return ""
    return f"{INSTRUMENT_REF_PREFIX}{numeric}"


def decode_instrument_ref(instrument_ref: str) -> int | None:
    """Recover the IBKR contract id from an opaque reference.

    ``None`` means the reference is not one of ours or is unusable, and every
    caller must fail closed rather than guess a contract from a ticker.
    """
    if not instrument_ref or not instrument_ref.startswith(INSTRUMENT_REF_PREFIX):
        return None
    body = instrument_ref[len(INSTRUMENT_REF_PREFIX) :].strip()
    try:
        numeric = int(body)
    except ValueError:
        return None
    return numeric if numeric > 0 else None


__all__ = [
    "INSTRUMENT_REF_PREFIX",
    "decode_instrument_ref",
    "encode_instrument_ref",
]
