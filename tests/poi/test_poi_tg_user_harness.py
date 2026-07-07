"""POI-SOC-TG-USER harness drills (transport faked; no Telegram contact).

These prove the harness invariants — read-only allowlist, session
persistence/fail-closed via IMP-0303, no-prompt verify, dedup, flood-wait
restraint, secret hygiene — so the only thing left for the real experiment
is the owner's login on a host with a TDLib binary. They do NOT mark the
PoI PASS: the real-connection criteria remain BLOCKED until then.
"""

from __future__ import annotations

import importlib.util
import sys
from collections import deque
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from trading_config.secrets import FileSecretStore
from trading_config.sessions import RuntimeSessionStore
from trading_contracts.enums import Environment

REPO_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "poi_tg_user", REPO_ROOT / "scripts" / "poi_tg_user.py"
)
assert _SPEC is not None and _SPEC.loader is not None
harness = importlib.util.module_from_spec(_SPEC)
sys.modules["poi_tg_user"] = harness
_SPEC.loader.exec_module(harness)


class FakeTransport:
    """Scripted TDLib: queued incoming events + captured outgoing requests."""

    def __init__(self, events: list[dict[str, Any]] | None = None) -> None:
        self.incoming: deque[dict[str, Any]] = deque(events or [])
        self.sent: list[dict[str, Any]] = []
        self.responders: list[Any] = []

    def send(self, request: dict[str, Any]) -> None:
        self.sent.append(request)
        for responder in self.responders:
            for event in responder(request) or []:
                self.incoming.append(event)

    def receive(self, timeout_seconds: float) -> dict[str, Any] | None:
        return self.incoming.popleft() if self.incoming else None


def auth_state(name: str) -> dict[str, Any]:
    return {"@type": "updateAuthorizationState", "authorization_state": {"@type": name}}


@pytest.fixture()
def stores(tmp_path: Path) -> tuple[FileSecretStore, RuntimeSessionStore]:
    secrets_dir = tmp_path / "secrets" / "RESEARCH"
    (secrets_dir / "telegram").mkdir(parents=True)
    (secrets_dir / "telegram" / "api_id").write_text("12345", encoding="utf-8")
    (secrets_dir / "telegram" / "api_hash").write_text("not-a-real-hash", encoding="utf-8")
    (secrets_dir / "sessions").mkdir()
    (secrets_dir / "sessions" / "fernet_key").write_text(
        Fernet.generate_key().decode("ascii"), encoding="utf-8"
    )
    secrets = FileSecretStore(tmp_path / "secrets", Environment.RESEARCH)
    sessions = RuntimeSessionStore(
        tmp_path / "sessions", Environment.RESEARCH, secrets.get("sessions/fernet_key")
    )
    return secrets, sessions


def make_poi(
    transport: FakeTransport,
    stores: tuple[FileSecretStore, RuntimeSessionStore],
    tmp_path: Path,
    *,
    prompts: dict[str, str] | None = None,
) -> Any:
    answers = prompts or {}

    def prompt_secret(label: str) -> str:
        for key, value in answers.items():
            if key in label:
                return value
        raise AssertionError(f"unexpected prompt: {label}")

    return harness.TelegramUserPoi(
        transport,
        stores[0],
        stores[1],
        tdlib_dir=tmp_path / "tdlib",
        prompt=prompt_secret,
        prompt_secret=prompt_secret,
        sleep=lambda _s: None,
        receive_timeout=0.01,
    )


LOGIN_FLOW = [
    auth_state("authorizationStateWaitTdlibParameters"),
    auth_state("authorizationStateWaitPhoneNumber"),
    auth_state("authorizationStateWaitCode"),
    auth_state("authorizationStateWaitPassword"),
    auth_state("authorizationStateReady"),
]


def test_login_persists_encrypted_key_and_sends_no_secret_in_clear_logs(
    tmp_path: Path, stores: tuple[FileSecretStore, RuntimeSessionStore], capsys: Any
) -> None:
    transport = FakeTransport(list(LOGIN_FLOW))
    poi = make_poi(
        transport,
        stores,
        tmp_path,
        prompts={"phone": "+000000", "code": "11111", "2FA": "hunter2"},
    )
    poi.login()
    # session stored encrypted; loadable
    _meta, key = stores[1].load(harness.CONNECTOR_ID, now=harness.utc_now())
    assert len(key) == 32
    # nothing sensitive in stdout
    out = capsys.readouterr().out.lower()
    for leaked in ("hunter2", "11111", "+000000", "not-a-real-hash"):
        assert leaked not in out
    # session file on disk does not contain the raw key bytes
    session_files = list((tmp_path / "sessions" / "RESEARCH").glob("*.session"))
    assert len(session_files) == 1
    assert key not in session_files[0].read_bytes()


def test_verify_uses_stored_session_without_prompts(
    tmp_path: Path, stores: tuple[FileSecretStore, RuntimeSessionStore]
) -> None:
    stores[1].save(harness.CONNECTOR_ID, b"k" * 32, now=harness.utc_now())
    transport = FakeTransport(
        [auth_state("authorizationStateWaitTdlibParameters"), auth_state("authorizationStateReady")]
    )
    poi = make_poi(transport, stores, tmp_path)  # no prompt answers: any prompt raises
    poi.verify()
    params = next(r for r in transport.sent if r["@type"] == "setTdlibParameters")
    assert params["api_id"] == 12345


def test_verify_fails_closed_without_session(
    tmp_path: Path, stores: tuple[FileSecretStore, RuntimeSessionStore]
) -> None:
    poi = make_poi(FakeTransport(), stores, tmp_path)
    with pytest.raises(harness.ReauthenticationRequired, match="AUTH_REQUIRED"):
        poi.verify()


def test_verify_fails_closed_on_corrupt_session(
    tmp_path: Path, stores: tuple[FileSecretStore, RuntimeSessionStore]
) -> None:
    stores[1].save(harness.CONNECTOR_ID, b"k" * 32, now=harness.utc_now())
    session_file = next((tmp_path / "sessions" / "RESEARCH").glob("*.session"))
    session_file.write_bytes(b"tampered")
    poi = make_poi(FakeTransport(), stores, tmp_path)
    with pytest.raises(harness.ReauthenticationRequired, match="AUTH_REQUIRED"):
        poi.verify()


def test_verify_never_answers_interactive_states(
    tmp_path: Path, stores: tuple[FileSecretStore, RuntimeSessionStore]
) -> None:
    """A stored session that still demands a login code is AUTH_REQUIRED."""
    stores[1].save(harness.CONNECTOR_ID, b"k" * 32, now=harness.utc_now())
    transport = FakeTransport(
        [
            auth_state("authorizationStateWaitTdlibParameters"),
            auth_state("authorizationStateWaitCode"),
        ]
    )
    poi = make_poi(transport, stores, tmp_path)
    with pytest.raises(harness.ReauthenticationRequired, match="no silent re-login"):
        poi.verify()


def test_read_only_allowlist_blocks_mutating_methods(
    tmp_path: Path, stores: tuple[FileSecretStore, RuntimeSessionStore]
) -> None:
    poi = make_poi(FakeTransport(), stores, tmp_path)
    for forbidden in ("sendMessage", "joinChat", "deleteMessages", "editMessageText"):
        with pytest.raises(harness.ReadOnlyViolation):
            poi._send({"@type": forbidden})
    assert "sendMessage" not in harness.ALLOWED_METHODS


def test_read_messages_normalizes_dedups_and_drops_content(
    tmp_path: Path, stores: tuple[FileSecretStore, RuntimeSessionStore]
) -> None:
    stores[1].save(harness.CONNECTOR_ID, b"k" * 32, now=harness.utc_now())
    message = {
        "@type": "message",
        "id": 777,
        "chat_id": -100,
        "date": 1_780_000_000,
        "sender_id": {"@type": "messageSenderUser", "user_id": 42},
        "content": {"@type": "messageText", "text": {"text": "secret tip: buy XYZ"}},
    }
    transport = FakeTransport(
        [auth_state("authorizationStateWaitTdlibParameters"), auth_state("authorizationStateReady")]
    )

    def responder(request: dict[str, Any]) -> list[dict[str, Any]]:
        extra = request.get("@extra")
        if request["@type"] == "loadChats":
            return [{"@type": "ok", "@extra": extra}]
        if request["@type"] == "getChat":
            return [{"@type": "chat", "id": -100, "title": "t", "@extra": extra}]
        if request["@type"] == "getChatHistory":
            return [{"@type": "messages", "messages": [message, message], "@extra": extra}]
        return []

    transport.responders.append(responder)
    poi = make_poi(transport, stores, tmp_path)
    records = poi.read_messages(-100, 5)
    assert len(records) == 1  # duplicate suppressed
    assert poi.duplicates_suppressed == 1
    record = records[0]
    assert record.message_id == 777
    assert record.chat_ref == "chat:-100"
    assert record.sender_ref.endswith(":42")
    assert record.content_kind == "messageText"
    assert record.content_length == len("secret tip: buy XYZ")
    # raw content is not retained anywhere on the record
    assert "XYZ" not in repr(record)


def test_flood_wait_is_single_and_bounded(
    tmp_path: Path, stores: tuple[FileSecretStore, RuntimeSessionStore]
) -> None:
    stores[1].save(harness.CONNECTOR_ID, b"k" * 32, now=harness.utc_now())
    waits: list[float] = []
    transport = FakeTransport(
        [auth_state("authorizationStateWaitTdlibParameters"), auth_state("authorizationStateReady")]
    )
    calls = {"n": 0}

    def responder(request: dict[str, Any]) -> list[dict[str, Any]]:
        extra = request.get("@extra")
        if request["@type"] == "loadChats":
            calls["n"] += 1
            if calls["n"] == 1:
                return [
                    {
                        "@type": "error",
                        "code": 429,
                        "message": "Too Many Requests: retry after 120",
                        "@extra": extra,
                    }
                ]
            return [{"@type": "ok", "@extra": extra}]
        if request["@type"] == "getChat":
            return [{"@type": "chat", "id": -1, "title": "t", "@extra": extra}]
        if request["@type"] == "getChatHistory":
            return [{"@type": "messages", "messages": [], "@extra": extra}]
        return []

    transport.responders.append(responder)
    poi = harness.TelegramUserPoi(
        transport,
        stores[0],
        stores[1],
        tdlib_dir=tmp_path / "tdlib",
        prompt=lambda _l: "",
        prompt_secret=lambda _l: "",
        sleep=waits.append,
        receive_timeout=0.01,
    )
    poi.read_messages(-1, 1)
    assert waits == [30.0]  # capped at FLOOD_WAIT_CAP_SECONDS, single wait
    assert calls["n"] == 2  # exactly one retry, no aggressive loop


def test_live_environment_rejected(tmp_path: Path) -> None:
    secrets = FileSecretStore(tmp_path / "secrets", Environment.LIVE)
    key = Fernet.generate_key().decode("ascii")
    from trading_config.secrets import SecretValue

    sessions = RuntimeSessionStore(tmp_path / "s", Environment.LIVE, SecretValue(key))
    with pytest.raises(harness.PoiError, match="LIVE is disabled"):
        harness.TelegramUserPoi(
            FakeTransport(),
            secrets,
            sessions,
            tdlib_dir=tmp_path / "tdlib",
            prompt=lambda _l: "",
            prompt_secret=lambda _l: "",
        )


def test_build_poi_requires_explicit_roots(monkeypatch: Any) -> None:
    for var in ("SECRETS_ROOT", "SESSIONS_ROOT", "TDLIB_DIR"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(harness.PoiError, match="SECRETS_ROOT"):
        harness.build_poi(FakeTransport())
