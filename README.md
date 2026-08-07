# taiwan-stock-lab-python

[![Tests](https://img.shields.io/badge/tests-35%20passed-4CAF50)](#testing)
[![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org)
[![Architecture](https://img.shields.io/badge/Architecture-Layered%20Domain%20Design-4CAF50)](#architecture)
[![Data](https://img.shields.io/badge/Data-PostgreSQL%20%2B%20Raw%20Snapshots-336791?logo=postgresql&logoColor=white)](#data-pipeline)
[![Scheduling](https://img.shields.io/badge/Scheduling-GitHub%20Actions-2088FF?logo=githubactions&logoColor=white)](#git-workflow--cicd)
[![Roadmap](https://img.shields.io/badge/Roadmap-LINE%20Delivery%20Next-06C755?logo=line&logoColor=white)](#roadmap)
[![Testing](https://img.shields.io/badge/Testing-pytest-0A9EDC?logo=pytest&logoColor=white)](#testing)
[![style: strategy-versioned](https://img.shields.io/badge/config-strategy--versioned-B22C89.svg)](#configuration)

---

## Introduction

A daily-batch quantitative research pipeline that scans Taiwan Stock
Exchange (TWSE) and Taipei Exchange (TPEx) common stocks for the day's
limit-up (漲停) closes, filters them through a configurable risk
policy, scores the survivors with a transparent multi-factor model,
and produces a research-only Top 5 shortlist. Built with a layered
domain design so that data ingestion, business rules, scoring, and
delivery can each be tested and evolved independently.

This project is for research, learning, and personal technical
practice. It is **not** investment advice, and the generated reports
are designed to say so explicitly (see [Disclaimer](#disclaimer)).

---

## Related Backend

This is currently a self-contained batch pipeline with no separate
backend service — data flows from public market data sources into
PostgreSQL and out to LINE. A dashboard/admin backend may be added in
a later phase (see [Roadmap](#roadmap)).

---

## Features

### Data Pipeline

- Trading-day determination that accounts for weekends and holidays,
  not just a Monday-Friday assumption
- Multi-source ingestion skeleton (FinMind as the primary aggregator,
  TWSE / TPEx / MOPS as official cross-check sources) behind a common
  `MarketDataProvider` protocol
- Every raw API response is snapshotted before any parsing or cleaning
  happens, keyed by a fresh `ingestion_run_id` per run — reruns never
  overwrite a previous snapshot
- Source-date verification (`StaleDataError`) to avoid silently
  reusing the previous trading day's data
- Legally correct tick-size table and limit-up price calculation using
  `Decimal` arithmetic, verified against the official TWSE worked
  example (reference price 40.60 → limit-up price 44.65) and against a
  tick-by-tick walk-up reference implementation across every tick-size
  band boundary

### Candidate Pool

- Filters to active common stocks only (ETFs, ETNs, warrants, and
  depositary receipts are excluded at this layer)
- Hard exclusion on missing required fields or data-quality failures
- Minimum turnover threshold, sorted by turnover descending, capped at
  50 candidates
- Distinguishes close-limit-up (`is_close_limit_up`, used for
  selection) from intraday-touch-limit-up (`has_touched_limit_up`,
  recorded only) — a stock that opened at limit-up and was later sold
  off is not the same signal as one that closed locked at limit-up

### Risk Policy

- Hard exclusions (disposition stocks, managed/full-cash-delivery
  stocks) kept separate from soft risk flags (attention stock, KY
  stock, one-price limit-up, excessive consecutive limit-up days,
  elevated 5-day return) that are recorded but do not remove a
  candidate from consideration
- Every threshold lives in a versioned `RiskPolicyConfig`
  (`config/strategy-v1.yaml`) rather than being hardcoded

### Multi-Factor Scoring

- Six transparent, equally-documented factors: liquidity, volume/price
  structure, momentum, institutional flow, fundamentals, and risk
  quality
- Cross-sectional percentile normalization (Winsorized 5%-95%) so
  factors on different units and scales become comparable
- A dedicated non-monotonic momentum scoring function — an already
  extended rally is treated as elevated chase-in risk, not as an
  automatically higher score
- Missing factors are never backfilled with a neutral score; the total
  score is renormalized over the factors that are actually available,
  and `data_completeness` is recorded per stock
- Stocks below the minimum data-completeness threshold are ineligible
  for the Top 5, regardless of score

### Delivery Safety

- GitHub Actions concurrency group prevents the three scheduled
  attempts (16:17 / 16:47 / 17:17 Taiwan time) or a manual trigger from
  running over each other
- Job-level idempotency check placeholder to skip a run entirely once
  a ranking and delivery have already succeeded for the day

---

## Roadmap

- **Phase 4** — LINE delivery: a fixed-template report renderer (no
  LLM required), two intentionally separate idempotency mechanisms — a
  SHA-256 **database idempotency key** (trading date + strategy
  version + target + message version) for duplicate-delivery
  detection, and a UUID **`X-Line-Retry-Key`** per HTTP push attempt
  for safely retrying a single in-flight call — and a LINE Messaging
  API client with per-status-code retry semantics (5xx retried with
  backoff, 409 treated as already-processed, 429 never auto-retried).
  The renderer and idempotency logic are designed to be fully
  unit-testable against `httpx.MockTransport` before any real channel
  token is issued.
- **Phase 5** — Performance tracking: T+1 / T+5 / T+20 returns, a fill
  simulation model that distinguishes signal return from assumed-fill
  return, and transaction cost modeling
- **Phase 6** — LLM-assisted report writing on top of the Phase 4
  rule-based renderer, with strict JSON-schema validation and
  automatic fallback to the fixed template on any validation failure
- **Phase 7** — Productionization: Cloud Scheduler + Cloud Run Job,
  and an optional web dashboard for historical ranking queries

---

## Disclaimer

Every generated report includes, verbatim:

> This list is generated from public market data and fixed
> quantitative rules. It is for research and data organization
> purposes only and does not constitute a recommendation to buy, sell,
> or hold any security.

Promotional or advisory language ("must buy," "hot tip," "guaranteed
profit," "best pick") is explicitly excluded and covered by tests
(`tests/test_text_renderer.py`).

---

## Tech Stack

- **Python 3.12** — `dataclasses`, `Protocol`, and `StrEnum` used
  throughout to keep domain models explicit and dependency-light
- **Decimal** — all price and tick-size arithmetic uses `Decimal`,
  never `float`, to avoid floating-point rounding errors in financial
  comparisons
- **pandas** — cross-sectional percentile normalization for
  multi-factor scoring
- **httpx** — HTTP client for market-data ingestion and the LINE
  Messaging API client; tested via `httpx.MockTransport` without any
  real network call
- **tenacity** — exponential-backoff retry policy for transient LINE
  API failures
- **SQLAlchemy** — ORM models for trading calendar, ingestion runs,
  raw source payload snapshots, and cleaned daily prices
- **PyYAML** — versioned strategy configuration (`config/strategy-v1.yaml`)
- **pytest** — unit tests across domain logic, risk policy, scoring,
  report rendering, and the LINE client
- **GitHub Actions** — scheduled daily job with `concurrency` guards
  and manual `workflow_dispatch` trigger for backfilling a specific
  trading date

---

## Environment

- Python: `3.12+`
- PostgreSQL: for the raw snapshot, clean data, and ranking tables
  (Phase 1 currently ships an in-memory repository for local testing;
  see [Roadmap](#roadmap))

---

## Local Development

```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

### Useful commands

```bash
pytest -v                          # run the full test suite
pytest -x --tb=short               # fail fast, short traceback (mirrors CI)
python -m app.jobs.daily_ranking   # run the Phase 1 ingestion job locally
```

### Configuration

Strategy thresholds and factor weights are centralized in
`config/strategy-v1.yaml` and never hardcoded into domain logic.
When tuning thresholds after backtesting, create a new
`strategy_version` (e.g. `rule-v1.1.0`) rather than overwriting the
existing file, so historical ranking results keep a stable reference
baseline.

Environment variables consumed by the job (see
`.github/workflows/daily-limit-up-ranking.yml`):

| Variable              | Purpose                                           |
| ---------------------- | -------------------------------------------------- |
| `FINMIND_TOKEN`        | FinMind API token                                  |
| `DATABASE_URL`         | PostgreSQL connection string                       |
| `TARGET_TRADING_DATE`  | Manual override for `workflow_dispatch` backfills  |

---

## Testing

```bash
pytest -v
```

- Unit tests for tick-size bands, the official TWSE limit-up worked
  example, and a tick-by-tick walk-up reference implementation used to
  regression-test every tick-size boundary crossing
- Candidate-pool tests covering instrument-type exclusion, minimum
  turnover exclusion, sort-and-cap-at-50 behavior, and non-limit-up
  exclusion
- Risk-policy tests covering hard exclusion, default soft flagging,
  policy-driven exclusion, and threshold-driven flags (consecutive
  limit-up days, 5-day return)
- Scoring tests covering factor-weight integrity, liquidity ordering,
  missing-factor renormalization (never backfilled with a neutral
  score), and Top-5 selection under a data-completeness floor
- Report-renderer tests asserting the disclaimer is always present and
  that promotional language never appears in output
- LINE client tests using `httpx.MockTransport` to simulate 200 / 409
  / 429 / 500 responses, verifying retry-key propagation, duplicate
  handling, and retry/backoff behavior — no real token required

35 tests currently passing across Phase 1-3 (data pipeline, candidate
pool, risk policy, and scoring).

---

## Architecture

Clean separation between data ingestion, domain rules, and delivery:

```
app/domain/        Pure business logic — no I/O, no framework dependency
  price_ticks.py      Tick-size table + limit-up price calculation
  limit_up.py          Close-limit-up vs touched-limit-up detection
  models.py             StockMaster / DailyPrice, decoupled from any data source
  candidate_builder.py  Common-stock filtering + turnover ranking
  risk_policy.py         Hard exclusion vs soft risk flagging
  features.py / normalization.py / scoring.py
                         Multi-factor scoring and Top-5 selection

app/ingestion/      Data source integration
  trading_calendar.py    Trading-day checks + stale-data guard
  providers.py             MarketDataProvider protocol, multi-source merge
  market_data_client.py    Raw-snapshot-first ingestion clients

app/db/              SQLAlchemy ORM models
app/jobs/            Scheduled job entry points
```

`app/reports/` (report rendering) and `app/clients/` (LINE Messaging
API client + idempotency) are planned for Phase 4 — see
[Roadmap](#roadmap).

---

## Git Workflow & CI/CD

- Feature-branch workflow: one branch per phase (e.g.
  `feature/phase1-3-mvp-pipeline`), merged into `main` via Pull
  Request once its test suite is green
- Commit messages follow a structured, phase-scoped summary format so
  `git log` doubles as a changelog of what each phase delivered
- GitHub Actions runs the daily ranking job on a three-attempt
  schedule (16:17 / 16:47 / 17:17 Taiwan time) with a `concurrency`
  group to prevent overlapping runs, plus a `workflow_dispatch` input
  for manually backfilling a specific trading date
- Secrets (`FINMIND_TOKEN`, `DATABASE_URL`, LINE channel credentials)
  are injected via GitHub Actions secrets and never committed to the
  repository

---

## Project Structure

> High-level overview, not an exhaustive file listing.

```
taiwan-stock-lab-python
├─ app
│  ├─ domain              # Pure business logic (see Architecture above)
│  ├─ ingestion             # Market data providers and raw snapshot clients
│  ├─ db                    # SQLAlchemy ORM models
│  └─ jobs                  # Scheduled job entry points
├─ config
│  └─ strategy-v1.yaml     # Versioned candidate/risk/factor-weight configuration
├─ tests                    # Mirrors app/, one module per domain component
├─ .github
│  └─ workflows
│     └─ daily-limit-up-ranking.yml
├─ requirements.txt
└─ README.md
```

---

## Credits

This project is created for independent learning and demonstration
purposes.

---

## Notes

All market data references (TWSE, TPEx, FinMind, MOPS) are used for
research and educational purposes only. Verify current rules, quotas,
and licensing terms directly with each provider before relying on this
project for anything beyond personal research.

---

## License

This repository is intended for learning and demonstration.

If you plan to open-source it, please choose a license and confirm
that any public distribution or paid offering built on top of it has
been separately reviewed for compliance with securities investment
advisory regulations in your jurisdiction — a disclaimer alone does
not satisfy that requirement.