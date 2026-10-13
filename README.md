# Employee Attendance & Analytics API

FastAPI/PyMongo implementation of HROne contract v2.0. Requires Python 3.11+ and MongoDB 6.0+; tested with Python 3.11.15 and MongoDB **6.0.29 / 7.0.28**.

All application code is in `app/main.py`. Synchronous routes run in FastAPI's worker threads with a shared PyMongo client. Unique indexes arbitrate duplicate inserts; conditional updates protect punch-out and append correction history atomically. UTC BSON instants become epoch milliseconds at the boundary; attendance dates use IST and rounding is half-up.

Analytics run entirely in MongoDB: employee-based lookups retain no-log headcounts, sums/counts produce record-weighted averages, `$rank` preserves cutoff ties, and generated calendar rows feed seven-day windows. Explain shares production query builders.

## Run

```sh
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI='mongodb://127.0.0.1:27017'
export MONGO_DB='attendance_db'
uvicorn app.main:app --port 8000
```

Startup creates nine secondary indexes idempotently; no seed step is required. `GET /health` returns 200 after database ping/index readiness, otherwise 503. Environment variables override optional `.env` settings.

## Verify

```sh
pip install -r requirements-dev.txt
export TEST_MONGO_URI='mongodb://127.0.0.1:27017'
export CANDIDATE_KIT='/absolute/path/to/candidate_kit'
python -m pytest -q
python -m tests.benchmark
ruff check . && ruff format --check .
```

Tests own disposable UUID databases; use a development server. Missing configuration explicitly skips database/original-sample tests. Verified: **115 tests per server**, plus **19 benchmark shapes** over 100,000 logs with matching results and indexed winning plans.

[VALIDATION.md](VALIDATION.md) contains commands, measurements, prior contract-audit evidence and limitations. [REVIEW.md](REVIEW.md) covers starter defects; [DECISIONS.md](DECISIONS.md) answers the five design questions. Deep offsets, exact totals and long histories remain scaling costs; reports are not snapshot transactions.
