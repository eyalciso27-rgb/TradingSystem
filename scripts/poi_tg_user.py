"""POI-SOC-TG-USER harness — Telegram personal user account via TDLib.

PoI spike ONLY (not a production adapter). Canonical path per CLAUDE.md /
Integration spec section 33: the owner's Telegram USER account through TDLib;
the Bot API is a separate connector and never a substitute or fallback.

Security invariants enforced here:

- ``api_id``/``api_hash`` are read only through the IMP-0302 FileSecretStore
  (runtime secret files) — never argv, env dumps, code or logs.
- The TDLib database is encrypted by TDLib itself with a random per-connector
  ``database_encryption_key``; that key exists on disk only inside the
  IMP-0303 RuntimeSessionStore (Fernet blob). Phone number, one-time login
  code and 2FA password are prompted interactively without echo and are never
  stored or logged.
- Read-only: every outgoing TDLib request must be on the explicit allowlist
  below. Nothing is sent to chats, joined, deleted or modified.
- Fail-closed: a missing/corrupt/expired stored key means re-authentication
  (AUTH_REQUIRED); there is no fallback. LIVE is rejected outright.
- FLOOD_WAIT (rate limit) is respected by a single bounded wait — no
  aggressive retry.

Runtime requirements (per host): a TDLib ``tdjson`` shared library
(``TDJSON_PATH`` env var or on the loader path). TDLib builds officially on
Windows x64 and Linux ARM64; this harness has zero pip dependencies beyond
the repo itself, so no amd64-only wheel is involved.

Subcommands: ``login`` (interactive, once), ``verify`` (loads the encrypted
session; must reach Ready without any prompt), ``read --chat-id N``
(read-only ingestion of a limited number of messages from a chat the account
is already a member of), ``revoke`` (per-connector revocation).
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import hashlib
import json
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from getpass import getpass
from pathlib import Path
from typing import Final, Protocol

from trading_config.secrets import FileSecretStore, SecretValue
from trading_config.sessions import RuntimeSessionStore, SessionError, utc_now
from trading_contracts.enums import Environment

CONNECTOR_ID: Final[str] = "telegram-user-tdlib"
SECRET_API_ID: Final[str] = "telegram/api_id"
SECRET_API_HASH: Final[str] = "telegram/api_hash"
SECRET_SESSION_KEY: Final[str] = "sessions/fernet_key"

# The complete set of TDLib methods this harness may ever send. Read-only by
# construction: no send/join/delete/edit method is present.
ALLOWED_METHODS: Final[frozenset[str]] = frozenset(
    {
        "setTdlibParameters",
        "checkDatabaseEncryptionKey",
        "setAuthenticationPhoneNumber",
        "checkAuthenticationCode",
        "checkAuthenticationPassword",
        "getAuthorizationState",
        "getMe",
        "loadChats",
        "getChats",
        "getChat",
        "getChatHistory",
        "setLogVerbosityLevel",
        "close",
    }
)

FLOOD_WAIT_CAP_SECONDS: Final[float] = 30.0


class PoiError(Exception):
    """Harness failure; messages never contain secret material."""


class ReadOnlyViolation(PoiError):
    """A non-allowlisted (potentially mutating) TDLib method was attempted."""


class ReauthenticationRequired(PoiError):
    """Stored session unusable or login state reached during verify."""


class TdTransport(Protocol):
    """Minimal tdjson JSON interface (injectable for tests)."""

    def send(self, request: dict[str, object]) -> None: ...

    def receive(self, timeout_seconds: float) -> dict[str, object] | None: ...


class TdjsonTransport:
    """ctypes binding to the real tdjson shared library."""

    def __init__(self, library_path: str) -> None:
        lib = ctypes.CDLL(library_path)
        lib.td_create_client_id.restype = ctypes.c_int
        lib.td_send.argtypes = [ctypes.c_int, ctypes.c_char_p]
        lib.td_receive.restype = ctypes.c_char_p
        lib.td_receive.argtypes = [ctypes.c_double]
        self._lib = lib
        self._client_id = int(lib.td_create_client_id())

    def send(self, request: dict[str, object]) -> None:
        payload = json.dumps(request).encode("utf-8")
        self._lib.td_send(self._client_id, payload)

    def receive(self, timeout_seconds: float) -> dict[str, object] | None:
        raw = self._lib.td_receive(ctypes.c_double(timeout_seconds))
        if not raw:
            return None
        decoded: dict[str, object] = json.loads(raw.decode("utf-8"))
        return decoded


def load_real_transport() -> TdTransport:
    library_path = os.environ.get("TDJSON_PATH") or ctypes.util.find_library("tdjson")
    if not library_path:
        raise PoiError(
            "tdjson library not found: set TDJSON_PATH to the built TDLib "
            "shared library (tdjson.dll / libtdjson.so)"
        )
    return TdjsonTransport(library_path)


PromptFn = Callable[[str], str]


@dataclass(frozen=True, slots=True)
class MessageRecord:
    """Read-only, non-sensitive record of one ingested message.

    The message text itself is NOT retained — only a content hash and length,
    which is all the PoI needs to prove capture and dedup.
    """

    message_id: int
    chat_ref: str
    occurred_at: int
    sender_ref: str
    content_kind: str
    content_sha256: str
    content_length: int


class TelegramUserPoi:
    """Auth state machine + read-only ingestion against a TdTransport."""

    def __init__(
        self,
        transport: TdTransport,
        secrets: FileSecretStore,
        sessions: RuntimeSessionStore,
        *,
        tdlib_dir: Path,
        prompt: PromptFn,
        prompt_secret: PromptFn,
        sleep: Callable[[float], None] = time.sleep,
        receive_timeout: float = 10.0,
    ) -> None:
        if secrets.environment is Environment.LIVE:
            raise PoiError("LIVE is disabled; the PoI never runs against LIVE")
        self._transport = transport
        self._secrets = secrets
        self._sessions = sessions
        self._tdlib_dir = tdlib_dir
        self._prompt = prompt
        self._prompt_secret = prompt_secret
        self._sleep = sleep
        self._timeout = receive_timeout
        self._extra = 0
        self.seen: set[tuple[str, int]] = set()
        self.duplicates_suppressed = 0

    # -- transport helpers ---------------------------------------------------

    def _send(self, request: dict[str, object]) -> None:
        method = str(request.get("@type"))
        if method not in ALLOWED_METHODS:
            raise ReadOnlyViolation(f"method {method!r} is not allowlisted (read-only PoI)")
        self._transport.send(request)

    def _request(self, request: dict[str, object]) -> dict[str, object]:
        """Send one request and wait for its @extra-matched reply."""
        self._extra += 1
        extra = f"poi-{self._extra}"
        self._send({**request, "@extra": extra})
        resent = False
        deadline = time.monotonic() + self._timeout * 6
        while time.monotonic() < deadline:
            event = self._transport.receive(self._timeout)
            if event is None:
                continue
            if event.get("@extra") == extra:
                if event.get("@type") == "error":
                    self._handle_error(event)  # raises unless it was a flood wait
                    if resent:
                        raise PoiError("rate limited twice; giving up (no aggressive retry)")
                    resent = True
                    self._send({**request, "@extra": extra})
                    continue
                return event
            self._dispatch_update(event)
        raise PoiError("timed out waiting for TDLib response")

    def _handle_error(self, event: dict[str, object]) -> None:
        message = str(event.get("message", ""))
        code = event.get("code")
        if code == 429 or "Too Many Requests" in message or "FLOOD_WAIT" in message:
            wait = min(_retry_after_seconds(message), FLOOD_WAIT_CAP_SECONDS)
            print(f"rate limited by Telegram; waiting {wait:.0f}s (single bounded wait)")
            self._sleep(wait)
            return
        raise PoiError(f"TDLib error {code}: {message}")

    def _dispatch_update(self, event: dict[str, object]) -> None:
        if event.get("@type") == "updateAuthorizationState":
            state = event.get("authorization_state")
            if isinstance(state, dict):
                self._pending_auth_state = str(state.get("@type"))

    _pending_auth_state: str | None = None

    # -- auth ----------------------------------------------------------------

    def login(self) -> None:
        """Interactive first login; persists the encryption key via IMP-0303."""
        key = os.urandom(32)
        self._run_auth(database_key=key, allow_prompts=True)
        self._sessions.rotate(CONNECTOR_ID, key, now=utc_now())
        print("login complete; TDLib database key stored encrypted (IMP-0303)")

    def verify(self) -> None:
        """Load the stored session; must reach Ready without any prompt."""
        try:
            _meta, key = self._sessions.load(CONNECTOR_ID, now=utc_now())
        except SessionError as error:
            raise ReauthenticationRequired(f"AUTH_REQUIRED: {error}") from error
        self._run_auth(database_key=key, allow_prompts=False)
        print("session verified: authorizationStateReady without re-login")

    def revoke(self) -> None:
        revoked = self._sessions.revoke(CONNECTOR_ID)
        print(f"session revoked: {revoked} (other connectors untouched)")

    def _run_auth(self, *, database_key: bytes, allow_prompts: bool) -> None:
        api_id = self._secrets.get(SECRET_API_ID)
        api_hash = self._secrets.get(SECRET_API_HASH)
        self._send({"@type": "setLogVerbosityLevel", "new_verbosity_level": 1})
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            event = self._transport.receive(self._timeout)
            if event is None:
                continue
            if event.get("@type") == "error":
                self._handle_error(event)
                continue
            if event.get("@type") != "updateAuthorizationState":
                continue
            state = event.get("authorization_state")
            state_type = str(state.get("@type")) if isinstance(state, dict) else ""
            if state_type == "authorizationStateReady":
                return
            if state_type == "authorizationStateClosed":
                raise PoiError("TDLib client closed before Ready")
            self._answer_auth_state(
                state_type,
                api_id=api_id,
                api_hash=api_hash,
                database_key=database_key,
                allow_prompts=allow_prompts,
            )
        raise PoiError("authorization did not reach Ready in time")

    def _answer_auth_state(
        self,
        state_type: str,
        *,
        api_id: SecretValue,
        api_hash: SecretValue,
        database_key: bytes,
        allow_prompts: bool,
    ) -> None:
        if state_type == "authorizationStateWaitTdlibParameters":
            self._send(
                {
                    "@type": "setTdlibParameters",
                    "database_directory": str(self._tdlib_dir),
                    "use_message_database": True,
                    "use_secret_chats": False,
                    "api_id": int(api_id.reveal()),
                    "api_hash": api_hash.reveal(),
                    "system_language_code": "en",
                    "device_model": "TradingSystem PoI",
                    "application_version": "0.1",
                    "database_encryption_key": _b64(database_key),
                }
            )
            return
        if state_type == "authorizationStateWaitEncryptionKey":
            self._send(
                {"@type": "checkDatabaseEncryptionKey", "encryption_key": _b64(database_key)}
            )
            return
        prompts: dict[str, tuple[str, str, str]] = {
            "authorizationStateWaitPhoneNumber": (
                "setAuthenticationPhoneNumber",
                "phone_number",
                "phone number (international format): ",
            ),
            "authorizationStateWaitCode": (
                "checkAuthenticationCode",
                "code",
                "one-time login code: ",
            ),
            "authorizationStateWaitPassword": (
                "checkAuthenticationPassword",
                "password",
                "2FA password: ",
            ),
        }
        if state_type in prompts:
            if not allow_prompts:
                raise ReauthenticationRequired(
                    f"stored session requires interactive step {state_type}; "
                    "AUTH_REQUIRED (no silent re-login)"
                )
            method, field, label = prompts[state_type]
            value = self._prompt_secret(label)  # never echoed, never logged
            self._send({"@type": method, field: value})
            return
        # Unknown intermediate states are ignored; TDLib will re-emit.

    # -- read-only ingestion ---------------------------------------------------

    def read_messages(self, chat_id: int, limit: int) -> list[MessageRecord]:
        """Read up to ``limit`` messages from a chat the account is in."""
        self.verify()
        self._request({"@type": "loadChats", "limit": 20})
        chat = self._request({"@type": "getChat", "chat_id": chat_id})
        title_hash = hashlib.sha256(str(chat.get("title", "")).encode()).hexdigest()[:12]
        print(f"chat ok: id={chat_id} title_sha={title_hash} (title not retained)")
        history = self._request(
            {
                "@type": "getChatHistory",
                "chat_id": chat_id,
                "from_message_id": 0,
                "offset": 0,
                "limit": limit,
                "only_local": False,
            }
        )
        raw_messages = history.get("messages")
        records: list[MessageRecord] = []
        if isinstance(raw_messages, list):
            for message in raw_messages:
                if isinstance(message, dict):
                    record = self._normalize(message)
                    if record is not None:
                        records.append(record)
        return records

    def _normalize(self, message: dict[str, object]) -> MessageRecord | None:
        message_id = int(str(message.get("id", 0)))
        chat_ref = f"chat:{message.get('chat_id')}"
        dedup_key = (chat_ref, message_id)
        if dedup_key in self.seen:
            self.duplicates_suppressed += 1
            return None
        self.seen.add(dedup_key)
        content = message.get("content")
        kind, text = "unknown", ""
        if isinstance(content, dict):
            kind = str(content.get("@type", "unknown"))
            inner = content.get("text")
            if isinstance(inner, dict):
                text = str(inner.get("text", ""))
        sender = message.get("sender_id")
        sender_ref = "unknown"
        if isinstance(sender, dict):
            sender_ref = f"{sender.get('@type')}:{sender.get('user_id', sender.get('chat_id'))}"
        return MessageRecord(
            message_id=message_id,
            chat_ref=chat_ref,
            occurred_at=int(str(message.get("date", 0))),
            sender_ref=sender_ref,
            content_kind=kind,
            content_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            content_length=len(text),
        )


def _b64(data: bytes) -> str:
    import base64

    return base64.b64encode(data).decode("ascii")


def _retry_after_seconds(message: str) -> float:
    for token in message.replace(":", " ").split():
        if token.isdigit():
            return float(token)
    return 5.0


def _environment() -> Environment:
    raw = os.environ.get("TRADING_ENVIRONMENT", "RESEARCH").upper()
    environment = Environment(raw)
    if environment is Environment.LIVE:
        raise PoiError("LIVE is disabled")
    return environment


def build_poi(transport: TdTransport | None = None) -> TelegramUserPoi:
    secrets_root = os.environ.get("SECRETS_ROOT")
    sessions_root = os.environ.get("SESSIONS_ROOT")
    tdlib_dir = os.environ.get("TDLIB_DIR")
    if not (secrets_root and sessions_root and tdlib_dir):
        raise PoiError(
            "SECRETS_ROOT, SESSIONS_ROOT and TDLIB_DIR environment variables "
            "are required (no defaults; secrets never live in the repo)"
        )
    environment = _environment()
    secrets = FileSecretStore(Path(secrets_root), environment)
    sessions = RuntimeSessionStore(
        Path(sessions_root), environment, secrets.get(SECRET_SESSION_KEY)
    )
    return TelegramUserPoi(
        transport if transport is not None else load_real_transport(),
        secrets,
        sessions,
        tdlib_dir=Path(tdlib_dir) / environment.value,
        prompt=input,
        prompt_secret=getpass,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="poi_tg_user", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("login")
    sub.add_parser("verify")
    read = sub.add_parser("read")
    read.add_argument("--chat-id", type=int, required=True)
    read.add_argument("--limit", type=int, default=5)
    sub.add_parser("revoke")
    args = parser.parse_args(argv)

    poi = build_poi()
    if args.command == "login":
        poi.login()
    elif args.command == "verify":
        poi.verify()
    elif args.command == "revoke":
        poi.revoke()
    else:
        records = poi.read_messages(args.chat_id, args.limit)
        for record in records:
            print(
                f"message_id={record.message_id} chat={record.chat_ref} "
                f"at={record.occurred_at} sender={record.sender_ref} "
                f"kind={record.content_kind} sha256={record.content_sha256[:16]} "
                f"len={record.content_length}"
            )
        print(
            f"read {len(records)} messages; duplicates suppressed: "
            f"{poi.duplicates_suppressed}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
