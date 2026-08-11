"""Static safety guardrails for the IBC auto-login assets (no execution).

Asserts the shell scripts, systemd units and IBC config example enforce
paper-only, credential-safe, no-order behavior — and that no real credential
is ever committed to the repo.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = REPO_ROOT / "scripts" / "install_ibc_gateway.sh"
START_SH = REPO_ROOT / "scripts" / "start_ibc_gateway.sh"
CONFIG_EXAMPLE = REPO_ROOT / "scripts" / "ibc" / "config.example.ini"
IBC_SERVICE = REPO_ROOT / "systemd" / "ibgateway-ibc.service"
WATCHDOG_SERVICE = REPO_ROOT / "systemd" / "ibgateway-watchdog.service"
WATCHDOG_TIMER = REPO_ROOT / "systemd" / "ibgateway-watchdog.timer"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# -- shell scripts ---------------------------------------------------------------


def test_scripts_are_strict_bash() -> None:
    for path in (INSTALL_SH, START_SH):
        text = read(path)
        assert text.startswith("#!/usr/bin/env bash")
        assert "set -euo pipefail" in text


def test_install_pins_version_and_verifies_checksum() -> None:
    text = read(INSTALL_SH)
    assert 'IBC_VERSION="3.24.1"' in text
    assert "d99ee28cc3539e3843fb00d28dc484c255006b0063d38f0808ac3ef07dd88bb8" in text
    assert "sha256sum --check" in text
    # checksum is verified BEFORE the extraction step
    assert text.index("sha256sum --check") < text.index("unzip -o")


def test_start_script_is_paper_only_and_never_leaks_credentials() -> None:
    text = read(START_SH)
    # paper-only gate present; live refused
    assert "TradingMode" in text and "paper" in text
    assert "refusing (paper only)" in text or "paper only" in text
    # credentials never on the command line: no --user=/--pw= with a value
    assert "--pw=" not in text
    assert "--user=" not in text
    # never prints the config / credentials
    assert "cat " not in text
    # enforces chmod 600 + owner
    assert '"600"' in text
    # no order/broker verbs
    for fragment in ("placeOrder", "cancelOrder", "reqGlobalCancel", "OrderIntent"):
        assert fragment not in text


def test_start_script_execs_foreground_ibcstart_not_gatewaystart() -> None:
    """Regression (systemd Type=simple): the launcher must exec the FOREGROUND
    IBC service script (scripts/ibcstart.sh) directly. IBC's gatewaystart.sh
    stays foreground only with a leading '-inline'; otherwise it backgrounds an
    xterm and returns, so the unit's main process exited immediately and the
    service reported 'Deactivated successfully.' while nothing ran."""
    text = read(START_SH)
    # Inspect CODE only — a comment may legitimately name gatewaystart.sh to
    # explain why it is avoided.
    code = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    # The backgrounding wrapper must NOT be invoked anywhere in the code.
    assert "gatewaystart.sh" not in code
    # The foreground service script is the entrypoint, exec'd so systemd tracks
    # the real Gateway process, with the major version as its first argument.
    assert 'entry="${IBC_PATH}/scripts/ibcstart.sh"' in code
    assert 'exec "${entry}" "${TWS_MAJOR_VRSN}"' in code
    # The Gateway is not re-backgrounded: the exec is not detached.
    assert "exec ${entry} &" not in code and 'exec "${entry}" &' not in code
    # Paper-only + IBC ini wiring preserved; still no credentials on argv.
    assert "--mode=paper" in code
    assert "--ibc-ini=${IBC_INI}" in code
    assert "--pw=" not in code and "--user=" not in code
    # Fail-closed: on a 2FA timeout IBC must EXIT, never self-restart. Blind
    # Gateway recovery is forbidden (unit is Restart=no); recovery is
    # owner-authorized only.
    assert "--on2fatimeout=exit" in code
    assert "--on2fatimeout=restart" not in code


def test_no_hardcoded_credentials_in_scripts() -> None:
    for path in (INSTALL_SH, START_SH):
        text = read(path).lower()
        # no assignment of a real password/login literal
        for fragment in ("ibpassword=", "ibloginid="):
            assert fragment not in text


# -- IBC config example ----------------------------------------------------------


def test_config_example_is_paper_and_has_empty_credentials() -> None:
    text = read(CONFIG_EXAMPLE)
    assert "TradingMode=paper" in text
    # credentials MUST be blank in the example
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("IbLoginId="):
            assert stripped == "IbLoginId="
        if stripped.startswith("IbPassword="):
            assert stripped == "IbPassword="


def test_config_example_has_mandatory_ibc_settings() -> None:
    text = read(CONFIG_EXAMPLE)
    required = [
        "TradingMode=paper",
        "ReloginAfterSecondFactorAuthenticationTimeout=yes",
        "SecondFactorAuthenticationExitInterval=",
        "AcceptNonBrokerageAccountWarning=yes",
        "AcceptIncomingConnectionAction=reject",
        "OverrideTwsApiPort=4002",
        "BindAddress=127.0.0.1",
    ]
    for setting in required:
        assert setting in text, setting
    assert "TradingMode=live" not in text.lower().replace(" ", "")


# -- systemd units ---------------------------------------------------------------


def test_ibc_service_is_paper_and_watchdog_managed() -> None:
    text = read(IBC_SERVICE)
    assert "User=opc" in text
    assert "/config.ini" in text  # points at the secure config
    # watchdog is the single restart manager
    assert "Restart=no" in text
    # no credential in a visible Environment= entry
    assert "IbPassword" not in text
    assert "IbLoginId" not in text
    assert "--pw=" not in text


def test_watchdog_units_present_and_safe() -> None:
    service = read(WATCHDOG_SERVICE)
    timer = read(WATCHDOG_TIMER)
    assert "ib_gateway_watchdog.py" in service
    assert "--port 4002" in service
    # waiting states are treated as success (not failures)
    assert "SuccessExitStatus=" in service
    assert "OnUnitActiveSec=" in timer
    assert "reconciliation-marker" in service


# -- the committed-secret guardrail ----------------------------------------------


def test_ibc_no_committed_secrets() -> None:
    """No tracked file may carry a non-empty IbLoginId/IbPassword, and no real
    config.ini may be committed."""
    offenders: list[str] = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        parts = set(path.parts)
        if parts & {".git", "node_modules", ".venv", "__pycache__", ".mypy_cache"}:
            continue
        if path.suffix.lower() not in {".ini", ".conf", ".cfg", ".env", ".txt", ".example"} and (
            path.name != "config.example.ini"
        ):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(("IbLoginId=", "IbPassword=")):
                value = stripped.split("=", 1)[1].strip()
                if value:  # a non-empty credential value must never be committed
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: {stripped.split('=')[0]}")
    assert not offenders, f"committed credential(s): {offenders}"


def test_no_real_config_ini_committed() -> None:
    for path in REPO_ROOT.rglob("config.ini"):
        if ".git" in path.parts:
            continue
        pytest.fail(f"a real config.ini is committed: {path.relative_to(REPO_ROOT)}")
