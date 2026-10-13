# Employee Attendance & Analytics API

Contract v2.0 attendance service with FastAPI, PyMongo, and MongoDB aggregation. All application code is in `app/main.py`. Requires Python 3.11+ and MongoDB 6.0+; validated on MongoDB 7.0.28 with Python 3.11.15.

## Run

```sh
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI='mongodb://127.0.0.1:27017'
export MONGO_DB='attendance_db'
uvicorn app.main:app --port 8000
```

Check readiness: `curl -i http://127.0.0.1:8000/health`. It returns 200 after a successful database ping and index initialization, otherwise 503. Startup creates indexes idempotently against externally seeded `employees` and `attendance_logs`; no seed step is required. Failed startup initialization is retried before readiness or writes. Real environment variables override optional local `.env` settings. Never commit credentials.

## Verify

```sh
pip install -r requirements-dev.txt
python -m pytest -q tests/test_rules.py
TEST_MONGO_URI='mongodb://127.0.0.1:27017' python -m pytest -q
TEST_MONGO_URI='mongodb://127.0.0.1:27017' python -m tests.benchmark
ruff check .
ruff format --check .
```

Integration and benchmark runs create fresh `hrone_test_<random UUID>` databases and delete only those databases. They never drop `MONGO_DB` or a user-supplied database name. Use a development server whose account may create/drop disposable databases. Without `TEST_MONGO_URI`, database tests explicitly skip. The original sample test additionally requires `CANDIDATE_KIT=/absolute/path/to/candidate_kit`; it runs the supplied seed script only against its own disposable database. Sample dumps are not included.

## Design and limits

- BSON UTC datetimes are converted to integer milliseconds at the API boundary. Punch inputs are strict integers and truncated to seconds; shift/date calculations use IST.
- Unique indexes protect creation. Punch-out uses a conditional atomic update; corrections compare the observed record and history before `$set` plus `$push`. A concurrent loser receives 409.
- Analytics read stored values, join by `emp_code`, retain no-log employees, use competition ranking, and generate calendar rows and seven-day windows inside MongoDB. Decimal arithmetic implements half-up rounding.
- Explain uses the same list command and aggregation builders as production, at `executionStats` verbosity. Plans depend on actual data and MongoDB version; measured results are in `VALIDATION.md`.
- Offset pagination and exact totals cost more on deep/large pages. Corrections compare the complete history array; very long histories will increase write cost and remain subject to MongoDB's document-size limit. No hidden grader run or MongoDB 6.0 runtime certification is claimed. A Starlette test-client deprecation warning is benign with the pinned test dependencies.

See `REVIEW.md` for starter defects and `DECISIONS.md` for the five requested design answers.
