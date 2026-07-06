"""Static secret loading tests (IMP-0302).

Done criteria: secret scan and log tests pass — no secret value ever appears
in repr/str/exceptions/logs; missing secrets fail closed; PAPER and LIVE
stores are fully separate. Fixture values are obviously fake.
"""

import json
import logging
from pathlib import Path

import pytest
from trading_config.secrets import (
    FileSecretStore,
    MissingSecretError,
    SecretPathError,
)
from trading_contracts.enums import Environment

FAKE = "fake-test-value-not-a-real-secret"


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    (tmp_path / "PAPER" / "broker").mkdir(parents=True)
    (tmp_path / "LIVE").mkdir(parents=True)
    (tmp_path / "PAPER" / "broker" / "account").write_text(FAKE + "\n", encoding="utf-8")
    return tmp_path


@pytest.fixture()
def paper(root: Path) -> FileSecretStore:
    return FileSecretStore(root, Environment.PAPER)


def test_load_from_runtime_file(paper: FileSecretStore) -> None:
    value = paper.get("broker/account")
    assert value.reveal() == FAKE


def test_missing_secret_fails_closed_naming_ref_only(paper: FileSecretStore) -> None:
    with pytest.raises(MissingSecretError) as excinfo:
        paper.get("broker/api")
    assert "broker/api" in str(excinfo.value)
    assert FAKE not in str(excinfo.value)  # never a value in the error


def test_require_all_lists_every_missing_ref(paper: FileSecretStore) -> None:
    with pytest.raises(MissingSecretError) as excinfo:
        paper.require_all(["broker/account", "telegram/api", "news/key"])
    message = str(excinfo.value)
    assert "telegram/api" in message and "news/key" in message
    assert FAKE not in message


def test_no_leak_via_repr_str_format_or_json(paper: FileSecretStore) -> None:
    value = paper.get("broker/account")
    assert FAKE not in repr(value)
    assert FAKE not in str(value)
    assert FAKE not in f"{value}"
    with pytest.raises(TypeError):
        json.dumps({"secret": value})  # not accidentally serializable


def test_no_leak_via_logging(
    paper: FileSecretStore, caplog: pytest.LogCaptureFixture
) -> None:
    value = paper.get("broker/account")
    with caplog.at_level(logging.INFO):
        logging.getLogger("test").info("loaded secret: %s", value)
    assert FAKE not in caplog.text
    assert "***" in caplog.text


@pytest.mark.parametrize("bad", ["../escape", "a/../../b", "/abs", "c:evil", "a\\b", "~home"])
def test_path_traversal_rejected(paper: FileSecretStore, bad: str) -> None:
    with pytest.raises(SecretPathError):
        paper.get(bad)


def test_symlink_escape_rejected(root: Path, paper: FileSecretStore) -> None:
    outside = root.parent / "outside-secret"
    outside.write_text("outside", encoding="utf-8")
    link = root / "PAPER" / "linked"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks not permitted on this runner")
    with pytest.raises(SecretPathError):
        paper.get("linked")


def test_paper_and_live_stores_are_separate(root: Path, paper: FileSecretStore) -> None:
    live = FileSecretStore(root, Environment.LIVE)
    # The secret exists only under PAPER: LIVE must NOT fall back to it.
    with pytest.raises(MissingSecretError):
        live.get("broker/account")
    # And a LIVE-only secret is invisible to the PAPER store.
    (root / "LIVE" / "broker-live").write_text("fake-live", encoding="utf-8")
    assert live.get("broker-live").reveal() == "fake-live"
    with pytest.raises(MissingSecretError):
        paper.get("broker-live")


def test_stores_never_read_environment_variables(
    paper: FileSecretStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Env dumps must stay clean: an env var never satisfies a secret ref.
    monkeypatch.setenv("BROKER_API", "env-should-be-ignored")
    with pytest.raises(MissingSecretError):
        paper.get("BROKER_API")
