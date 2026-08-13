"""Per-source activation: probes, secret specs and owner runbooks (Track C).

One place that binds each merged adapter to the activation framework
(``activation.py``): the fail-closed secret-key convention, a READ-ONLY
preflight probe (built as a closure over the injected provider client, so tests
drive fakes and the owner drives the real client), and a declarative
``OwnerRunbook`` (exact owner-run commands, PASS/FAIL criteria, re-auth / logout
/ rotation procedures, and the auth/lifecycle states to exercise).

No real login happens here. Nothing constructs a real network client.
"""

from __future__ import annotations

from dataclasses import dataclass

from trading_contracts.enums import IntegrationHealthStatus

from trading_application.source_adapters.activation import (
    ActivationError,
    ClientKind,
    PreflightProbe,
    SecretSpec,
    assert_no_execution_surface,
    secret_key,
)
from trading_application.source_adapters.filings import FilingsAdapter, FilingSource
from trading_application.source_adapters.telegram_bot import TelegramBotAdapter, TelegramBotClient
from trading_application.source_adapters.telegram_user import (
    TdlibAuthState,
    TelegramUserAdapter,
    TelegramUserClient,
)
from trading_application.source_adapters.tradingview import TradingViewAdapter, verify_signature

_SOCIAL = "social"
_INFO = "info"

# -- secret specs (fail-closed naming convention) ----------------------------

TELEGRAM_USER_SPEC = SecretSpec(
    connector_id="telegram-user-tdlib",
    required_keys=(
        secret_key(_SOCIAL, "telegram-user-tdlib", "api_id"),
        secret_key(_SOCIAL, "telegram-user-tdlib", "api_hash"),
    ),
)
TELEGRAM_BOT_SPEC = SecretSpec(
    connector_id="telegram-bot",
    required_keys=(secret_key(_SOCIAL, "telegram-bot", "bot_token"),),
)
FILINGS_SPEC = SecretSpec(
    connector_id="news-filings",
    required_keys=(),  # SEC EDGAR is public; a provider key is optional
    optional_keys=(secret_key(_INFO, "news-filings", "provider_api_key"),),
)
TRADINGVIEW_SPEC = SecretSpec(
    connector_id="tradingview-webhook",
    required_keys=(secret_key(_INFO, "tradingview-webhook", "webhook_secret"),),
)


# -- probes (closures over the injected client) ------------------------------


def telegram_user_probe(client: TelegramUserClient) -> PreflightProbe:
    """READ-ONLY: map the TDLib authorization state to a health status."""

    def probe(secrets, client_kind):  # type: ignore[no-untyped-def]
        adapter = TelegramUserAdapter(
            client, connector_id="telegram-user-tdlib", source_account_id="owner",
            allowlist=[],
        )
        assert_no_execution_surface(adapter)
        state = client.authorization_state()
        mapping = {
            TdlibAuthState.READY: (IntegrationHealthStatus.HEALTHY, "READY user session"),
            TdlibAuthState.WAIT_PHONE: (IntegrationHealthStatus.AUTH_REQUIRED, "awaiting phone"),
            TdlibAuthState.WAIT_CODE: (IntegrationHealthStatus.AUTH_REQUIRED, "awaiting code"),
            TdlibAuthState.WAIT_PASSWORD: (
                IntegrationHealthStatus.AUTH_REQUIRED, "awaiting 2FA password"),
            TdlibAuthState.CLOSED: (IntegrationHealthStatus.DOWN, "session closed/revoked"),
        }
        return mapping.get(state, (IntegrationHealthStatus.DOWN, "unknown state"))

    return probe


def telegram_bot_probe(client: TelegramBotClient) -> PreflightProbe:
    """READ-ONLY: bot authorization. The bot never satisfies the user gate."""

    def probe(secrets, client_kind):  # type: ignore[no-untyped-def]
        adapter = TelegramBotAdapter(
            client, connector_id="telegram-bot", source_account_id="bot", allowlist=[],
        )
        assert_no_execution_surface(adapter)
        if adapter.satisfies_user_requirement():  # must always be False
            raise ActivationError("telegram bot must never satisfy the user gate")
        if client.is_authorized():
            return IntegrationHealthStatus.HEALTHY, "bot authorized (supplemental only)"
        return IntegrationHealthStatus.AUTH_REQUIRED, "bot token not authorized"

    return probe


def filings_probe(sources: tuple[FilingSource, ...]) -> PreflightProbe:
    """READ-ONLY: reachability of each official/public source (no ingestion)."""

    def probe(secrets, client_kind):  # type: ignore[no-untyped-def]
        adapter = FilingsAdapter(sources)
        assert_no_execution_surface(adapter)
        for source in sources:
            try:
                source.fetch(after_cursor="")  # bounded read-only reachability check
            except Exception as error:  # noqa: BLE001 - probe reports DOWN, never raises up
                return (
                    IntegrationHealthStatus.DOWN,
                    f"{source.name} unreachable: {type(error).__name__}",
                )
        return IntegrationHealthStatus.HEALTHY, f"{len(sources)} source(s) reachable"

    return probe


def tradingview_probe(
    *, sample_body: bytes | None = None, sample_signature: str | None = None
) -> PreflightProbe:
    """READ-ONLY: verify webhook configuration; HEALTHY needs a real signed alert."""

    def probe(secrets, client_kind):  # type: ignore[no-untyped-def]
        secret_value = secrets[TRADINGVIEW_SPEC.required_keys[0]]
        adapter = TradingViewAdapter(secret=secret_value.reveal())
        assert_no_execution_surface(adapter)
        if sample_body is None or sample_signature is None:
            return (
                IntegrationHealthStatus.AUTH_REQUIRED,
                "webhook secret configured; awaiting a real signed alert",
            )
        if verify_signature(sample_body, sample_signature, secret_value.reveal()):
            return IntegrationHealthStatus.HEALTHY, "signed alert verified"
        return IntegrationHealthStatus.DEGRADED, "sample alert signature invalid"

    return probe


# -- owner runbooks ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OwnerRunbook:
    """Everything the owner needs to activate one source (no code required)."""

    connector_id: str
    poi_id: str
    secret_keys: tuple[str, ...]
    states: tuple[str, ...]
    owner_commands: tuple[str, ...]
    pass_criteria: tuple[str, ...]
    fail_criteria: tuple[str, ...]
    reauth_procedure: str
    logout_procedure: str
    rotation_procedure: str


_CLI = "uv run python -m trading_application.source_adapters.activation_cli"

TELEGRAM_USER_RUNBOOK = OwnerRunbook(
    connector_id="telegram-user-tdlib",
    poi_id="POI-SOC-TG-USER",
    secret_keys=TELEGRAM_USER_SPEC.required_keys,
    states=(
        "api_id/api_hash missing -> AUTH_REQUIRED (fail-closed)", "phone/QR auth",
        "login code required", "2FA password required", "READY", "revoked",
        "session corrupted", "reconnect", "logout", "encrypted local TDLib state",
    ),
    owner_commands=(
        "place api_id/api_hash in the RESEARCH/PAPER secret store under the keys above",
        f"{_CLI} check-secrets telegram-user --environment PAPER --root <secret-root>",
        "complete the interactive TDLib login (phone -> code -> 2FA) on a controlled host",
        "run the host preflight entrypoint with the REAL TDLib client; it writes a"
        " sanitized ActivationEvidence JSON to docs/poi/evidence/POI-SOC-TG-USER.json",
    ),
    pass_criteria=(
        "client_kind == REAL", "authorization state READY",
        "preflight status HEALTHY", "verdict PASS", "encrypted session persisted",
    ),
    fail_criteria=(
        "any missing secret", "state stuck at WAIT_* -> AUTH_REQUIRED",
        "CLOSED/revoked -> DOWN", "a FAKE client (always NOT_EVIDENCE)",
    ),
    reauth_procedure="re-run the interactive login; the mandatory user gate needs a READY session",
    logout_procedure="log out in Telegram; delete the encrypted TDLib session dir; state -> CLOSED",
    rotation_procedure="rotate api_id/api_hash in the secret store, then re-run preflight",
)

TELEGRAM_BOT_RUNBOOK = OwnerRunbook(
    connector_id="telegram-bot",
    poi_id="POI-SOC-TG-BOT",
    secret_keys=TELEGRAM_BOT_SPEC.required_keys,
    states=("bot_token missing -> AUTH_REQUIRED", "authorized", "revoked", "reconnect"),
    owner_commands=(
        "place bot_token in the secret store under the key above",
        "configure allowed chats (owner-controlled test chat)",
        f"{_CLI} check-secrets telegram-bot --environment PAPER --root <secret-root>",
        "run the host preflight entrypoint with the REAL Bot-API client; it writes"
        " docs/poi/evidence/POI-SOC-TG-BOT.json",
    ),
    pass_criteria=(
        "client_kind == REAL", "bot authorized", "status HEALTHY", "verdict PASS",
        "runs alongside the user adapter and CANNOT satisfy the user gate",
    ),
    fail_criteria=(
        "missing bot_token", "unauthorized -> AUTH_REQUIRED",
        "any attempt to satisfy the user gate (hard error)", "a FAKE client",
    ),
    reauth_procedure="issue a new bot token via BotFather and update the secret store",
    logout_procedure="revoke the bot token in BotFather; the connector reports AUTH_REQUIRED",
    rotation_procedure="rotate bot_token in the secret store, then re-run preflight",
)

FILINGS_RUNBOOK = OwnerRunbook(
    connector_id="news-filings",
    poi_id="POI-NEWS-001",
    secret_keys=FILINGS_SPEC.optional_keys,
    states=("public source reachable -> HEALTHY", "source outage -> DOWN", "rate limited"),
    owner_commands=(
        "select an approved/public source (e.g. the SEC EDGAR endpoint)",
        "(optional) place a provider_api_key in the secret store",
        f"{_CLI} check-secrets filings --environment PAPER --root <secret-root>",
        "run the host preflight entrypoint with the REAL source; it writes"
        " docs/poi/evidence/POI-NEWS-001.json",
    ),
    pass_criteria=(
        "client_kind == REAL", "source reachable", "status HEALTHY", "verdict PASS",
        "point-in-time + independent-source clustering preserved",
    ),
    fail_criteria=(
        "source outage -> DOWN", "malformed schema -> DEGRADED/circuit", "a FAKE source",
    ),
    reauth_procedure="n/a for public EDGAR; rotate provider_api_key if a keyed provider is used",
    logout_procedure="not applicable for public sources",
    rotation_procedure="rotate provider_api_key in the secret store if a keyed provider is used",
)

TRADINGVIEW_RUNBOOK = OwnerRunbook(
    connector_id="tradingview-webhook",
    poi_id="POI-TV-001",
    secret_keys=TRADINGVIEW_SPEC.required_keys,
    states=(
        "webhook_secret missing -> AUTH_REQUIRED", "configured; awaiting alert -> AUTH_REQUIRED",
        "signed alert verified -> HEALTHY", "bad signature -> DEGRADED",
    ),
    owner_commands=(
        "place webhook_secret in the secret store under the key above",
        "configure the TradingView alert webhook with the shared secret (HMAC-SHA256)",
        f"{_CLI} check-secrets tradingview --environment PAPER --root <secret-root>",
        "fire a real signed test alert to the receiver",
        "run the host preflight entrypoint with the real signed alert; it writes"
        " docs/poi/evidence/POI-TV-001.json",
    ),
    pass_criteria=(
        "client_kind == REAL", "a real signed alert verifies (constant-time HMAC)",
        "status HEALTHY", "verdict PASS", "replay/idempotency holds", "NO order path",
    ),
    fail_criteria=(
        "missing webhook_secret", "invalid/tampered signature -> DEGRADED",
        "no real alert -> AUTH_REQUIRED (never PASS)", "a FAKE probe",
    ),
    reauth_procedure="rotate the shared webhook secret and reconfigure the TradingView alert",
    logout_procedure="remove the webhook configuration in TradingView; the receiver rejects alerts",
    rotation_procedure="rotate webhook_secret in the secret store, then reconfigure the alert",
)

RUNBOOKS: dict[str, OwnerRunbook] = {
    "telegram-user": TELEGRAM_USER_RUNBOOK,
    "telegram-bot": TELEGRAM_BOT_RUNBOOK,
    "filings": FILINGS_RUNBOOK,
    "tradingview": TRADINGVIEW_RUNBOOK,
}

SPECS: dict[str, SecretSpec] = {
    "telegram-user": TELEGRAM_USER_SPEC,
    "telegram-bot": TELEGRAM_BOT_SPEC,
    "filings": FILINGS_SPEC,
    "tradingview": TRADINGVIEW_SPEC,
}


__all__ = [
    "FILINGS_RUNBOOK",
    "FILINGS_SPEC",
    "RUNBOOKS",
    "SPECS",
    "TELEGRAM_BOT_RUNBOOK",
    "TELEGRAM_BOT_SPEC",
    "TELEGRAM_USER_RUNBOOK",
    "TELEGRAM_USER_SPEC",
    "TRADINGVIEW_RUNBOOK",
    "TRADINGVIEW_SPEC",
    "ClientKind",
    "OwnerRunbook",
    "filings_probe",
    "telegram_bot_probe",
    "telegram_user_probe",
    "tradingview_probe",
]
