# Expense Sharing Service

![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-4169E1?logo=postgresql&logoColor=white)
![SQLAlchemy](https://img.shields.io/badge/SQLAlchemy-D71F00?logo=sqlalchemy&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-2496ED?logo=docker&logoColor=white)

[![CI](https://github.com/Vinayak150/expense-sharing-service/actions/workflows/ci.yml/badge.svg)](https://github.com/Vinayak150/expense-sharing-service/actions/workflows/ci.yml)

A FastAPI + PostgreSQL backend for shared expenses, immutable allocations and exact balances.
It supports equal or custom splits, smart settlement suggestions and idempotent recording of
user-reported repayments. Python 3.12+, Pydantic v2, synchronous SQLAlchemy 2, Psycopg 3 and
Alembic form a small modular monolith. No hosted dependencies are required.
Tests use pytest, HTTPX and Hypothesis; Ruff checks style and Docker Compose runs the demo.

**Core assignment:** users, groups, membership, shared expenses, equal splitting, expense
history, net balances and bilateral debts. **Required product enhancement:** smart settlement
suggestions. **Optional additions:** custom unequal splits, recorded repayments and durable
repayment idempotency.

## Run with Docker

```sh
git clone https://github.com/Vinayak150/expense-sharing-service.git
cd expense-sharing-service
cp .env.example .env
docker compose up --build -d --wait
docker compose run --rm migrate  # safe recheck; startup already applies migrations
curl --fail http://localhost:8000/health/ready
```

Docker Compose is the only required host tool for this path. PostgreSQL readiness precedes
the one-shot migration job; the API starts after that job succeeds. Open
[Swagger](http://localhost:8000/docs), [ReDoc](http://localhost:8000/redoc) or
[OpenAPI JSON](http://localhost:8000/openapi.json).

Both published ports bind to loopback. Demo credentials in `.env.example` are local examples;
keep your `.env` private. Use URL-safe credentials (letters, digits, underscore); manually
supplied URLs require encoding other characters. `docker compose down` preserves the named
database volume. **Do not use `down -v` on data you need.**

`/health` checks liveness. `/health/ready` runs `SELECT 1`, returning 503 on database outage;
it does not check schema revision. Migrations provide that separate startup gate.

## Develop and test

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -c requirements.lock '.[dev]'
cp .env.example .env  # only if you have not configured it already
docker compose up -d db
alembic upgrade head
uvicorn app.main:app --reload
```

`DATABASE_URL` is required via environment or `.env`, using synchronous Psycopg:
`postgresql+psycopg://expense_demo:local_demo_only@localhost:5432/expense_sharing`.
Compose supplies its own URL with hostname `db`; the example above is for host tools.

```sh
ruff check .
ruff format --check .
python -m pip check
TEST_DATABASE_URL=postgresql+psycopg://expense_demo:local_demo_only@localhost:5432/expense_sharing pytest
```

Tests create and drop random disposable **databases** on the configured server; they never
migrate or clear the database named in the URL. Use a dedicated server/role with `CREATEDB`
and permission to terminate its own sessions for outage tests. Without `TEST_DATABASE_URL`,
PostgreSQL tests explicitly skip; an unreachable configured server fails. HTTP integration
tests use real PostgreSQL. Concurrency tests coordinate independent sessions with events,
barriers and observed PostgreSQL lock waits. Hypothesis tests include independent financial
references. Audit tests also exercise deferred commit failures and exact-money boundaries.

`requirements.lock` pins runtime/dev transitive versions across platform markers; use `-c`
for installation. The build backend is pinned. To intentionally update the lock with uv 0.13.1:

```sh
uv pip compile pyproject.toml --all-extras --universal --python-version 3.12 --upgrade --output-file requirements.lock
```

uv is a maintainer tool, not an application dependency. Reinstall and rerun checks after updates.

## API

| Method | Path | Behavior |
| --- | --- | --- |
| POST / GET | `/users` | Create user / list by ID |
| POST | `/groups` | Create empty group |
| POST / GET | `/groups/{group_id}/members` | Add member / list by user ID |
| POST | `/expenses` | Create expense and all persisted allocations atomically |
| GET | `/groups/{group_id}/expenses` | History, newest timestamp/ID first |
| GET | `/groups/{group_id}/balances` | Member positions and labelled debt representation |
| GET | `/balances?group_id=1` | Same group-scoped balance result |
| GET | `/groups/{group_id}/settlement-suggestions` | Advisory netted payment routes |
| POST / GET | `/groups/{group_id}/settlements` | Report repayment / list history |

Body IDs are positive JSON integers within signed BIGINT range. Names are trimmed nonblank
strings of 1–100 characters; descriptions 1–500. Duplicate names are allowed and never merge
identities. Requests reject unknown fields. Missing resources return 404; duplicate membership
or changed idempotency payload returns 409; invalid inputs/business rules return 422.
Validation errors contain `detail: [{loc,msg,type}]`; domain errors contain
`detail: {code,message}`. Unexpected failures return sanitized 500, never empty success.

For the assignment's dinner example, create Mohit, Rahul and Ankit via `POST /users` with
`{"name":"Mohit"}` (and the other names). Create `POST /groups` with `{"name":"Goa Trip"}`;
add each returned user ID using `POST /groups/{group_id}/members` and `{"user_id":1}`.
Using the returned IDs, record:

```json
{"group_id":1,"paid_by":1,"amount":"1200","description":"Dinner"}
```

`POST /expenses` returns three ₹400 shares. The group's balance endpoint shows Rahul and
Ankit each owing Mohit ₹400; the suggestion endpoint returns the same two routes. Generated
IDs may differ. The balance demo script below runs this workflow without hard-coded IDs.

## Exact money and splitting

HTTP `amount` means **INR rupees**. Accept JSON numbers or decimal strings, preserving numeric
tokens as Decimal before validation. Expense totals and each repayment report support
₹0.01–₹1,000,000,000.00 (1–100,000,000,000 paise). Custom shares additionally allow zero.
Scientific notation and redundant zeros are accepted: `1.00`, `1.000` and `1e0` are equivalent.
`1.001`, tiny nonzero shares, booleans and nonfinite values are rejected, never rounded.
Decimal strings use ASCII syntax, no whitespace/underscores and at most 128 characters;
coefficients have at most 128 digits. Unrepresentable numeric exponents return 422.

Internally money is integer paise in BIGINT columns. SQL aggregates use exact NUMERIC, then
Python integers. Responses pair main amounts with exact two-decimal rupee strings. Repayment
contribution totals are integer paise. Aggregate values may exceed BIGINT or JavaScript's
2^53−1 safe range: clients need lossless JSON integer parsing (including IDs) and should use
paired rupee strings for display. No monetary computation passes through float.

Omitted/null `splits` means equal shares among **all current members**, including payer.
`divmod` distributes remainder paise to lowest user IDs. ₹0.02 among three gives 1/1/0 paise.
The payer receives their share but never owes themselves. Explicit custom allocations are a
nonempty list of distinct current members whose nonnegative amounts sum exactly to the total:

```json
{"group_id":1,"paid_by":1,"amount":"1000","description":"Hotel",
 "splits":[{"user_id":1,"amount":"500"},{"user_id":2,"amount":"300"},{"user_id":3,"amount":"200"}]}
```

Custom subsets may exclude the payer. Allocations are immutable snapshots: joining later
never changes old shares. Ordinary expense retries are **not idempotent** and may create
separate expenses; atomic writes guarantee complete snapshots, not duplicate prevention.

## Balances, suggestions and repayment reports

`net = expense paid − allocated shares + repayments sent − repayments received`.
Positive means receive; negative means owe. Every group's signed nets sum to zero.

Before any repayment, `debt_basis: "expense_pairwise"` reports participant-to-payer obligations
with opposite directions cancelled. It can retain historical circular obligations even when
all nets are zero; the smart preview correctly needs no transfers for that cycle. These
expense-pair edges are not mandatory payments authorized by current net positions.
After any repayment, `debt_basis: "net_after_repayments"` labels debts as a canonical net-based
plan, rather than original expense-pair provenance. Complete repayment yields zero nets,
empty debts and empty suggestions. Original expenses and repayment facts remain visible.

**Smart Settlement Suggestions** is the mandatory product enhancement. It answers who can
pay whom with fewer routes than historical expense pairs. For Hotel ₹900 paid by Mohit and
Taxi ₹300 paid by Rahul among three users, three bilateral edges become Rahul→Mohit ₹100
and Ankit→Mohit ₹400. It reduces coordination, not money owed.

The deterministic heuristic compares ID-ordered greedy and valid pairwise/component candidates
by route count, routed amount, then canonical sequence. It independently replays candidates;
the selected plan clears all nets, never exceeds the debt baseline count and uses at most
N−1 routes for N nonzero members. It **does not guarantee a global minimum**. See
[architecture](docs/architecture.md) for the fallback proof and concrete counterexamples.

Previews make no writes: `suggestions_recorded:false`. `has_recorded_settlements` describes
history; deprecated `payments_recorded:false` only means the GET made no payment write.
Routes aggregate amounts: a route above ₹1,000,000,000.00 requires multiple partial reports,
each with a distinct key. Counts describe routes, not POST requests. Re-fetch after changes.

```sh
curl --fail -X POST http://localhost:8000/groups/1/settlements \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: dinner-rahul-1' \
  -d '{"from_user_id":2,"to_user_id":1,"amount":"150.00"}'
```

Use actual generated IDs. Both users must be members; sender must currently owe and receiver
must currently be owed. Amount cannot exceed either outstanding magnitude; overpayments are
rejected, not capped. The group row lock protects revalidation and insertion in one transaction.

Keys are group-scoped, case-sensitive ASCII `[A-Za-z0-9._:-]`, 1–128 characters. New reports
return 201. The same key and canonical sender/receiver/paise replay the original row with 200,
even after full repayment or restart; a changed payload returns 409. Reuse a key for retries,
and use new keys for distinct partial reports. Outputs say `status:"payment_reported"` and
`payment_verified:false`.

## Architecture and migrations

`app/api` validates/routes, `schemas` defines contracts, `services` owns transactions and
financial logic, `models` maps tables, and `core` provides configuration/database/exact money.
Synchronous handlers use synchronous sessions; session dependencies never commit. All group
writers lock the group row first under READ COMMITTED, then read current facts. Balance and
preview reads use one REPEATABLE READ / READ ONLY snapshot. No mutable balance table exists.

Six tables hold users, groups, add-only memberships, expenses, snapshot splits and reported
settlements. Stable numeric IDs, composite membership/split keys, positive/nonnegative amount
checks, unique group/retry keys and restrictive foreign keys protect structural integrity.
All timestamps are aware UTC. History indexes match `(group_id,created_at DESC,id DESC)`.
Ordinary CHECK/FK constraints cannot enforce cross-row allocation sums or repayment caps;
cooperating application transactions enforce them. Direct privileged SQL can bypass business
rules or mutate history. See the [schema diagram and detailed tradeoffs](docs/architecture.md).

```sh
alembic upgrade head
alembic current
alembic check
alembic upgrade head --sql
```

Revisions `0001_foundation` and `0002_settlements` are preserved; no `create_all()` substitute.
Review autogeneration before applying future migrations. On disposable data only, test
`alembic downgrade base` then `alembic upgrade head`. Downgrading 0002 drops repayment facts;
downgrading to base drops all six tables. Never downgrade data you need.

## Demo and audit tools

After starting the API, these scripts append fictional scenarios without clearing prior data:

```sh
python scripts/verify_balances.py --base-url http://localhost:8000 --output outputs/balance-demo.json
python scripts/verify_settlements.py --base-url http://localhost:8000 --output outputs/preview-demo.json
python scripts/verify_ledger.py --base-url http://localhost:8000 --output outputs/ledger-demo.json
```

The balance script reproduces the three-member ₹1200 dinner with ₹400 shares. Generated IDs
make the scripts work in populated databases. Generated evidence stays in ignored `outputs/`.
[GitHub Actions](https://github.com/Vinayak150/expense-sharing-service/actions/workflows/ci.yml)
runs the complete PostgreSQL-backed test suite on PostgreSQL 16.2 and 17.6, migration checks,
Ruff, dependency consistency and wheel verification. No private secrets are needed by CI.

## Trust boundaries and future work

This is a **trusted demo** with no authentication or authorization, and is unsafe for untrusted
public users. Repayments are unverified user reports; there is no payment-provider integration
or guarantee that money actually moved. CORS is not opened globally. There is no exposed
financial update/delete API, member removal or rejoining contract.

Lists are unpaginated, retry keys are retained indefinitely, and long snapshots can retain old
row versions. No production-scale benchmark or throughput claim is made. Authentication,
pagination, repayment reversals and a complete membership lifecycle are future design work,
not implemented features. No frontend, Redis, background workers or deployment is required.
