from trading_market_data_core import dataset_v1


EVIDENCE = "docs/poi/evidence/POI-IBKR-HIST.json"
EXPECTED_CHECKSUM = "sha256:663150437934774a09c3a5d5b81614458b4da329dfb42df64f930533ab552945"
EMPIRICALLY_VERIFIED = {
    "connectivity_paper_du_gate",
    "daily_trades_rth_schema",
    "contract_identity_conid",
    "head_timestamp_coverage",
    "bid_ask_midpoint",
    "symbol_change_discovery",
}
BLOCKED_EXTERNAL = {
    "pit_sp500_membership",
    "asof_gics",
    "delisted_security_history",
    "corporate_actions",
}


def test_checked_in_owner_evidence_drives_dataset_capability_matrix() -> None:
    matrix = dataset_v1.DatasetCapabilityMatrix.from_ibkr_historical_evidence(EVIDENCE)

    assert matrix.evidence_checksum == EXPECTED_CHECKSUM
    for name in EMPIRICALLY_VERIFIED:
        assert matrix.states[name] is dataset_v1.CapabilityState.EMPIRICALLY_VERIFIED
    for name in BLOCKED_EXTERNAL:
        assert matrix.states[name] is dataset_v1.CapabilityState.BLOCKED_EXTERNAL
    assert not matrix.full_dataset_contract_ready
