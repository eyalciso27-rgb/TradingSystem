"""AI extraction tasks (IMP-0804) — safe task contracts over a mocked port.

Authority: Integration_Specification_v0.6 sections 8/11 and 33.1 (ModelProvider
port: budget, timeout, schema validation, no broker tools; invalid model
output => bounded retry or fallback ONLY when the task policy defines one),
Decision_Contract 12.1 (SourceEntryType — instruction semantics) and the
baseline invariants: LLM output is untrusted structured input and must pass
schema validation; no self-reported confidence is ever a decision gate; AI
never invokes tools or reaches broker/execution.

Per ADR-0014 the ONLY provider here is the deterministic MockModelProvider —
no real AI provider, no API keys, no network. Real benchmarking/calibration
waits for POI-AI-001.

Safety design:

- **Trust gate before the task**: input text passes IMP-0305 ``assess``
  first; flagged content (prompt injection, ...) BLOCKS the task — the
  provider is never called with adversarial input.
- **Strict schemas** (pydantic frozen, extra="forbid"): hallucinated or
  unknown fields — including any self-reported ``confidence`` or
  ``tool_calls`` — are rejected, never silently accepted.
- **Fail closed / fallback per policy**: a provider failure uses the
  fallback provider only if the task policy defines one (Integration 11);
  otherwise the task fails closed with PROVIDER_UNAVAILABLE. An invalid
  output is retried at most ``retry_limit`` times, then fails closed.
- **No tool surface**: this module has no eval/exec/subprocess/network and
  never creates a Candidate/Decision/Order.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, ValidationError
from trading_contracts.enums import SourceEntryType

from trading_application.trust import SourceClass as TrustSourceClass
from trading_application.trust import assess

SCHEMA_VERSION = "ai-task-v1"


class AiTaskError(Exception):
    """Fail-closed task-layer failure (bad policy/inputs)."""


class AiTaskType(StrEnum):
    """Canonical extraction tasks (IMP-0804 scope)."""

    ENTITY_EXTRACTION = "ENTITY_EXTRACTION"
    INSTRUCTION_EXTRACTION = "INSTRUCTION_EXTRACTION"
    CONTEXT_EXTRACTION = "CONTEXT_EXTRACTION"
    CRITIC_REVIEW = "CRITIC_REVIEW"


class _StrictOutput(BaseModel):
    """Frozen, extra-forbidden base: unknown/hallucinated fields rejected."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str


class EntityExtractionOutput(_StrictOutput):
    symbols: tuple[str, ...]


class InstructionExtractionOutput(_StrictOutput):
    entry_type: SourceEntryType  # canonical semantics (Decision_Contract 12.1)
    symbol: str | None = None
    reference_price: Decimal | None = None


class ContextExtractionOutput(_StrictOutput):
    topics: tuple[str, ...]
    catalyst_hint: str | None = None


class CriticReviewOutput(_StrictOutput):
    consistent: bool
    issues: tuple[str, ...] = ()


OUTPUT_SCHEMAS: dict[AiTaskType, type[_StrictOutput]] = {
    AiTaskType.ENTITY_EXTRACTION: EntityExtractionOutput,
    AiTaskType.INSTRUCTION_EXTRACTION: InstructionExtractionOutput,
    AiTaskType.CONTEXT_EXTRACTION: ContextExtractionOutput,
    AiTaskType.CRITIC_REVIEW: CriticReviewOutput,
}


class ModelProviderPort(Protocol):
    """Generic model-provider port (Integration 33.1). No single-provider
    coupling; adapters implement this. Returns the RAW (untrusted) output."""

    @property
    def provider_id(self) -> str: ...

    @property
    def model_id(self) -> str: ...

    @property
    def model_version(self) -> str: ...

    def complete(
        self, task_type: AiTaskType, input_text: str, *, schema_version: str
    ) -> dict[str, object]: ...


class MockModelProvider:
    """Deterministic fixture provider — the only provider under ADR-0014."""

    def __init__(self, provider_id: str = "mock-provider") -> None:
        self._provider_id = provider_id
        self._responses: dict[tuple[AiTaskType, str], dict[str, object]] = {}
        self.fail_next = False
        self.calls: list[tuple[AiTaskType, str]] = []

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def model_id(self) -> str:
        return "mock-model"

    @property
    def model_version(self) -> str:
        return "0.0.1"

    def script(
        self, task_type: AiTaskType, input_text: str, response: dict[str, object]
    ) -> None:
        self._responses[(task_type, input_text)] = response

    def complete(
        self, task_type: AiTaskType, input_text: str, *, schema_version: str
    ) -> dict[str, object]:
        self.calls.append((task_type, input_text))
        if self.fail_next:
            self.fail_next = False
            raise ConnectionError("simulated provider outage")
        try:
            return self._responses[(task_type, input_text)]
        except KeyError as error:
            raise LookupError(f"no scripted response for {task_type}") from error


@dataclass(frozen=True, slots=True)
class AiTaskPolicy:
    """Task policy — configuration input (retry, fallback, injection markers,
    golden-set acceptance threshold). No defaults for the gates."""

    policy_version: str
    injection_markers: tuple[str, ...]
    retry_limit: int
    allow_fallback: bool
    golden_set_min_pass_rate: Decimal

    def __post_init__(self) -> None:
        if not self.policy_version.strip():
            raise AiTaskError("policy_version is mandatory")
        if not self.injection_markers:
            raise AiTaskError("injection markers are mandatory (fail closed)")
        if self.retry_limit < 0:
            raise AiTaskError("retry_limit must be >= 0")
        if not (0 <= self.golden_set_min_pass_rate <= 1):
            raise AiTaskError("golden_set_min_pass_rate must be within [0, 1]")


class TaskStatus(StrEnum):
    VALID = "VALID"
    INVALID_OUTPUT = "INVALID_OUTPUT"
    BLOCKED = "BLOCKED"  # adversarial input; provider never called
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class AiTaskRequest:
    task_id: str
    task_type: AiTaskType
    input_ref: str
    input_text: str
    as_of: datetime
    source_class: TrustSourceClass = TrustSourceClass.SOCIAL_UNKNOWN

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")


@dataclass(frozen=True, slots=True)
class AiTaskResult:
    """Untrusted-structured-input wrapper: parsed output + full provenance.

    The result carries NO confidence gate: the model's opinion of itself is
    not a field here, and downstream calibration status stays UNVERIFIED
    until POI-AI-001 runs.
    """

    task_id: str
    task_type: AiTaskType
    input_ref: str
    as_of: datetime
    status: TaskStatus
    schema_version: str
    provider_id: str | None
    model_id: str | None
    model_version: str | None
    output: _StrictOutput | None
    reason_codes: tuple[str, ...]
    fallback_used: bool


class AiTaskRunner:
    """Runs extraction tasks safely against the provider port."""

    def __init__(
        self,
        provider: ModelProviderPort,
        policy: AiTaskPolicy,
        *,
        fallback_provider: ModelProviderPort | None = None,
    ) -> None:
        if fallback_provider is not None and not policy.allow_fallback:
            raise AiTaskError("fallback provider given but policy forbids fallback")
        self._provider = provider
        self._fallback = fallback_provider if policy.allow_fallback else None
        self._policy = policy

    def run(self, request: AiTaskRequest) -> AiTaskResult:
        # 1. Trust gate BEFORE the task: adversarial input never reaches a model.
        trust = assess(
            content_ref=request.input_ref,
            claimed_hash=request.input_ref,
            source_class=request.source_class,
            content_text=request.input_text,
            injection_markers=self._policy.injection_markers,
        )
        if trust.flags:
            return self._result(
                request,
                status=TaskStatus.BLOCKED,
                provider=None,
                output=None,
                reasons=tuple(flag.value for flag in trust.flags),
                fallback_used=False,
            )

        # 2. Provider call with policy-bounded fallback (Integration 11).
        raw, provider, fallback_used, unavailable_reasons = self._call_provider(request)
        if raw is None:
            return self._result(
                request,
                status=TaskStatus.PROVIDER_UNAVAILABLE,
                provider=provider,
                output=None,
                reasons=unavailable_reasons,
                fallback_used=fallback_used,
            )

        # 3. Strict schema validation: hallucinated fields fail closed.
        schema = OUTPUT_SCHEMAS[request.task_type]
        reasons: list[str] = list(unavailable_reasons)
        for attempt in range(self._policy.retry_limit + 1):
            try:
                output = schema.model_validate(raw)
            except ValidationError as error:
                reasons.append(f"SCHEMA_VIOLATION_ATTEMPT_{attempt + 1}")
                if attempt < self._policy.retry_limit:
                    raw = provider.complete(
                        request.task_type,
                        request.input_text,
                        schema_version=SCHEMA_VERSION,
                    )
                    continue
                reasons.append(f"INVALID_OUTPUT:{error.error_count()}_errors")
                return self._result(
                    request,
                    status=TaskStatus.INVALID_OUTPUT,
                    provider=provider,
                    output=None,
                    reasons=tuple(reasons),
                    fallback_used=fallback_used,
                )
            if output.schema_version != SCHEMA_VERSION:
                return self._result(
                    request,
                    status=TaskStatus.INVALID_OUTPUT,
                    provider=provider,
                    output=None,
                    reasons=(*reasons, "SCHEMA_VERSION_MISMATCH"),
                    fallback_used=fallback_used,
                )
            return self._result(
                request,
                status=TaskStatus.VALID,
                provider=provider,
                output=output,
                reasons=tuple(reasons) or ("OK",),
                fallback_used=fallback_used,
            )
        raise AssertionError("unreachable")  # pragma: no cover

    def _call_provider(
        self, request: AiTaskRequest
    ) -> tuple[dict[str, object] | None, ModelProviderPort, bool, tuple[str, ...]]:
        try:
            raw = self._provider.complete(
                request.task_type, request.input_text, schema_version=SCHEMA_VERSION
            )
            return raw, self._provider, False, ()
        except Exception:  # noqa: BLE001 - provider failure handled by policy
            if self._fallback is None:
                return None, self._provider, False, ("PROVIDER_UNAVAILABLE",)
            try:
                raw = self._fallback.complete(
                    request.task_type, request.input_text, schema_version=SCHEMA_VERSION
                )
                return raw, self._fallback, True, ("PRIMARY_PROVIDER_UNAVAILABLE",)
            except Exception:  # noqa: BLE001
                return (
                    None,
                    self._fallback,
                    True,
                    ("PRIMARY_PROVIDER_UNAVAILABLE", "FALLBACK_PROVIDER_UNAVAILABLE"),
                )

    def _result(
        self,
        request: AiTaskRequest,
        *,
        status: TaskStatus,
        provider: ModelProviderPort | None,
        output: _StrictOutput | None,
        reasons: tuple[str, ...],
        fallback_used: bool,
    ) -> AiTaskResult:
        return AiTaskResult(
            task_id=request.task_id,
            task_type=request.task_type,
            input_ref=request.input_ref,
            as_of=request.as_of,
            status=status,
            schema_version=SCHEMA_VERSION,
            provider_id=provider.provider_id if provider else None,
            model_id=provider.model_id if provider else None,
            model_version=provider.model_version if provider else None,
            output=output,
            reason_codes=reasons,
            fallback_used=fallback_used,
        )


def evaluate_golden_set(
    runner: AiTaskRunner,
    cases: tuple[tuple[AiTaskRequest, _StrictOutput], ...],
    *,
    min_pass_rate: Decimal,
) -> tuple[Decimal, bool]:
    """Golden-set acceptance: pass rate vs the POLICY threshold (DoD).

    The threshold is configuration input; with the mock provider this proves
    the acceptance mechanics only — real quality numbers wait for POI-AI-001.
    """
    if not cases:
        raise AiTaskError("golden set must not be empty")
    passed = 0
    for request, expected in cases:
        result = runner.run(request)
        if result.status is TaskStatus.VALID and result.output == expected:
            passed += 1
    rate = Decimal(passed) / Decimal(len(cases))
    return rate, rate >= min_pass_rate


__all__ = [
    "OUTPUT_SCHEMAS",
    "SCHEMA_VERSION",
    "AiTaskError",
    "AiTaskPolicy",
    "AiTaskRequest",
    "AiTaskResult",
    "AiTaskRunner",
    "AiTaskType",
    "ContextExtractionOutput",
    "CriticReviewOutput",
    "EntityExtractionOutput",
    "InstructionExtractionOutput",
    "MockModelProvider",
    "ModelProviderPort",
    "TaskStatus",
    "evaluate_golden_set",
]
