"""Broker port, capability model and conformance interfaces (IMP-1001).

- ``trading_broker_core.port`` — the canonical BrokerPort boundary
  (Integration 9.1), capability matrix (20.4) and error taxonomy.
- ``trading_broker_core.fake`` — deterministic in-memory fake broker for
  conformance/execution tests only (never a real adapter).
- ``trading_broker_core.conformance`` — the reusable suite every broker
  adapter must pass.

No IBKR/SDK import may ever appear in this package (guardrails).
"""

__all__: list[str] = []
