# Trading System (Monorepo)

Skeleton created by task **IMP-0001** of `docs/baseline-v0.5/CLAUDE_CODE_TASKS.yaml`.
No business logic exists yet. The authoritative specification baseline is
`docs/baseline-v0.5/` (see `CLAUDE.md`).

> **Live trading is prohibited.** Paper Auto and Live modes are disabled by
> baseline policy. The current approved activity is Phase 0 and isolated
> Proof-of-Integration work only.
>
> **ARM64 status:** IMPLEMENTED LOCALLY — CI WIRING AND ARM64 ACCEPTANCE
> PENDING. The target deployment platform is Oracle Linux ARM64; all skeleton
> tooling is ARM64-compatible, but no build has been executed on ARM64 yet.

## Layout

```
apps/
  api/               # Core API (Python; FastAPI arrives in its own task)
  core-worker/       # Python domain workers
  scheduler/         # Python scheduled jobs
  execution-agent/   # Python broker runtime
  connectors/        # Node/TypeScript connector runtime
  dashboard/         # Operations Console (Next.js arrives in PHASE-11)
packages/
  py/                # contracts, domain, application, config, persistence,
                     # event-bus, market-data-core, broker-core, broker-ibkr,
                     # observability
  ts/                # contracts-generated, connector-sdk, ui
config/              # assumptions, strategies, policies, prompts, sources,
                     # market-data, brokers (versioned configuration)
database/            # alembic migrations and seeds (arrive in PHASE-2)
deploy/              # Docker Compose stacks (arrive in IMP-0401)
tests/               # fixtures, contract, integration, replay, e2e, smoke
docs/                # specification baseline and project docs
```

## Install

Prerequisites: [uv](https://docs.astral.sh/uv/) and Node.js 24 LTS with Corepack.

```sh
uv sync                 # creates .venv with Python 3.13 and all workspace packages
corepack pnpm install   # installs the pnpm workspace
```

## Lint / typecheck / test

Python:

```sh
uv run ruff check .
uv run mypy
uv run pytest
```

Node/TypeScript:

```sh
corepack pnpm lint
corepack pnpm typecheck
corepack pnpm test
```

## Architecture guardrails

Machine-enforced rules from the baseline (IMP-0002):

- `ibapi` is allowed only inside `packages/py/broker-ibkr`; provider SDKs
  (FastAPI, SQLAlchemy, NATS, Playwright) only inside their adapter/app.
- `contracts` imports no internal packages; `domain` imports only `contracts`;
  the broker adapter is imported only by itself and `apps/execution-agent`.
- Canonical environments (`RESEARCH`/`PAPER`/`LIVE`) and operating modes are
  defined in `packages/py/config/src/trading_config/environments.py`. Unknown
  values fail explicitly; LIVE and PAPER_AUTO are prohibited by baseline;
  Paper and Live never share queue names or account allowlists.
- Forbidden patterns: hardcoded account ids, LIVE defaults, queue names mixing
  paper and live, embedded secrets, hardcoded Windows paths.

Run the guardrails:

```sh
uv run python scripts/check_architecture.py   # standalone checker
uv run pytest tests/architecture              # positive + negative tests
```

Architecture changes require an ADR (`docs/adr/ADR-0000-template.md`) and
user approval. Code ownership is defined in `.github/CODEOWNERS`.

## Baseline verification

The specification baseline in `docs/baseline-v0.5/` is hash-locked (IMP-0003):
`BASELINE_LOCK.json` pins the SHA-256 of every baseline file (CRLF normalized
to LF so hashes match across Windows and Linux), and
`CONTRACT_AUTHORITY_MAP.json` records which document is authoritative for
which contract area. Changing a baseline document without an explicit lock
update fails verification.

```sh
uv run python scripts/verify_baseline.py    # verify manifest, hashes, lock
uv run python scripts/build_metadata.py     # build record: versions + hashes
uv run pytest tests/baseline                # positive + negative tests
```

Regenerating the lock (`--update`) is an explicit, reviewed action.

## Hebrew setup notes

See `README_SETUP_HE.md` for the original Hebrew setup instructions.
