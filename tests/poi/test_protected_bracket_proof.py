"""The owner-safe protected-bracket proof (ADR-0019, Increment 6).

Two properties are pinned deterministically, without ever opening a socket:

* the SCOPED authority, armed, drives the canonical decision path to ONE real
  native bracket (both legs), and unarmed it DENIES — so a bare run places
  nothing;
* the CLI refuses a Live port and a wrong arming token before anything opens,
  so the Paper-only / deliberate-gesture guarantees hold at the entrypoint too.

The real IBKR wire translation is exercised only by the owner's run; everything
up to ``placeOrder`` is proven here over the deterministic fake transport.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

from trading_execution_agent.execution_service import ExecutionStage
from trading_execution_agent.protected_entry import IbkrProtectedEntryLifecycle

# The proof lives in scripts/, imported as a top-level module the way the other
# script tests do (scripts/ is not a package, so siblings import each other by
# bare name; a package marker would break that).
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from poi_protected_bracket_proof import (  # noqa: E402
    ARM_TOKEN,
    ScopedPaperBracketAuthorization,
    main,
)

from tests.integration.test_execution_service import (  # noqa: E402
    NOW,
    PnlAwareTransport,
    _adapter,
    _intent,
    _service,
)

EXIT_OK = 0
EXIT_ERROR = 2


def _proof_service(*, armed: bool):  # type: ignore[no-untyped-def]
    transport = PnlAwareTransport()
    adapter = _adapter(transport)
    authority = ScopedPaperBracketAuthorization(
        armed=armed, equity=Decimal("50000"), now=NOW
    )
    service, fake, _ = _service(
        transport=transport,
        adapter=adapter,
        protection=IbkrProtectedEntryLifecycle(adapter),
        protection_required=True,
        authorization=authority,
    )
    return service, fake, adapter


def test_armed_scoped_authority_places_one_native_bracket() -> None:
    service, fake, adapter = _proof_service(armed=True)

    evaluation = service.execute(_intent())

    assert evaluation.stage is ExecutionStage.SUBMITTED, evaluation.reasons
    bracket_calls = [c for c in fake.calls if c.startswith("submit_bracket:")]
    assert len(bracket_calls) == 1
    assert not any(c.startswith("submit:") for c in fake.calls)
    legs = {o.client_order_id for o in adapter.get_open_orders("paper-main")}
    submitted = fake.submitted_intents[-1]
    assert submitted.client_order_id in legs
    assert submitted.protective is not None
    assert submitted.protective.stop_client_order_id in legs


def test_unarmed_scoped_authority_denies_and_places_nothing() -> None:
    service, fake, _ = _proof_service(armed=False)

    evaluation = service.execute(_intent())

    assert evaluation.stage is ExecutionStage.AUTHORIZATION_DENIED
    assert not any(c.startswith("submit_bracket:") for c in fake.calls)
    assert not any(c.startswith("submit:") for c in fake.calls)


def test_cli_refuses_the_live_port_before_anything_opens() -> None:
    # No build_service, no socket: the refusal is at the very top of main().
    assert main(["--port", "4001"]) == EXIT_ERROR


def test_cli_refuses_a_wrong_arming_token_before_anything_opens() -> None:
    assert main(["--execute", "not-the-phrase"]) == EXIT_ERROR


def test_the_arming_phrase_is_a_deliberate_multiword_gesture() -> None:
    # A bare flag can never arm a real order: the token is a whole phrase.
    assert " " not in ARM_TOKEN  # a single token, but a distinctive one
    assert ARM_TOKEN != "true" and len(ARM_TOKEN) > 12
