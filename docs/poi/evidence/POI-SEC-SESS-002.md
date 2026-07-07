# POI-SEC-SESS-002 — Encrypted Runtime Session Store

**Status: PASS** (Windows x64 verified locally; ARM64 via the CI
`arm64-emulated` job, which runs the same drill under QEMU aarch64)

| Field | Value |
|---|---|
| Date | 2026-07-07 |
| Environment | local dev (PAPER-namespace store under pytest tmp dirs) |
| Platform | Windows-11-10.0.26200 x64; Python 3.13.12 |
| Versions | cryptography 46.0.7 (Fernet: AES-CBC + HMAC); trading-config @ workspace |
| Command | `uv run pytest tests/poi/test_poi_sec_sess_002.py -q` |
| Result | 6 passed (drill file: [test_poi_sec_sess_002.py](../../../tests/poi/test_poi_sec_sess_002.py)) |

The component under test is the real IMP-0303 `RuntimeSessionStore` — no part
of the store was mocked. Session material used in the drill is synthetic.

## Steps: expected vs. actual

| Step | Expected | Actual |
|---|---|---|
| Encrypt session at rest | on-disk blob contains no plaintext/hex material | PASS — raw bytes and hex encoding absent from the `.session` file |
| Restart adapter | fresh store instance over same root+key recovers blob | PASS — metadata + blob identical after "restart" |
| Rotate wrapping key | re-encrypt under new key; old key fails closed | PASS — new key loads; old key raises `CorruptSessionError` (no fallback) |
| Revoke one session | only the revoked connector is affected | PASS — `telegram-user` revoked; `whatsapp-user` loads intact |
| Verify logs redaction | session material never reaches log output | PASS — central redaction emits `[REDACTED]`; hex/plaintext absent |
| Fail-closed extras | missing → re-auth; tampered → corrupt (no leak in message); expired → re-auth; PAPER blob invisible to other env | PASS |

## Limitations

- ARM64 execution happens in the CI `arm64-emulated` job (QEMU), not on this
  Windows machine; per standing instructions CI results are checked by the
  human operator. The store is pure Python + `cryptography` (multi-arch
  wheels), so no arch-specific behavior is expected.
- Wrapping-key rotation is a documented procedure (load under old key →
  `rotate()` under new key); there is no single one-shot re-wrap helper yet.
- "Secrets absent from DB/domain tables" is enforced by design (domain tables
  hold only `session_ref`; static schema test forbids secret-named columns)
  — verified structurally, not against a live PostgreSQL here.
