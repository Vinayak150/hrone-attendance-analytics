# Validation evidence

Validated 14 October 2026. This reports executed checks, not hidden-grader results.

## Endpoint verification

| Method and path | Result |
|---|---|
| GET /health | Verified 200/503, real outage/recovery, index creation and restart |
| POST /employees | Verified schema/validation, BSON UTC, duplicates and concurrent creation |
| GET /employees | Verified exact department filter, sorting, pagination and total |
| POST /attendance/punch-in | Verified strict epochs, defaults, R1/R2, BSON and concurrent uniqueness |
| POST /attendance/punch-out | Verified duration boundaries, overnight completion, R3–R5 and atomic conflict |
| GET /attendance | Verified schema, legacy defaults, filters, sorting, pagination and 100k query plans |
| PATCH /attendance/{emp_code}/{date} | Verified corrections, nested BSON history, no-ops, absent/leave rules and CAS conflicts |
| GET /analytics/employees/{emp_code}/monthly | Verified independent reference, empty months, joiners, leap/calendar boundaries |
| GET /analytics/departments/summary | Verified headcounts without logs, future joiners, record-weighted averages and rounding |
| GET /analytics/leaderboard/late | Verified competition ranks, cutoff ties, department scope and orphan exclusion |
| GET /analytics/departments/{department}/trend | Verified all calendar rows, headcount changes, 92-day boundary and seven-row averages |
| GET /admin/explain/{endpoint} | Verified all five modes, shared production query structure and execution statistics |

## Tests and static checks

The previous submission's original suite passed 20 tests before changes. New tests reproduced an unhandled `OverflowError` for `month=9999-12`; it was fixed without rejecting that valid month. Subsequent validation passed **84 tests, zero failures, zero skips**, including the official seed script. The last clean-clone run completed in **12.73 seconds** using a freshly installed virtual environment. One Starlette TestClient/httpx deprecation warning remains; it does not affect assertions.

Executed from the clean checkout:

```sh
TEST_MONGO_URI=mongodb://127.0.0.1:27028 CANDIDATE_KIT=/absolute/path/to/candidate_kit ../clean-venv/bin/python -m pytest -q
../clean-venv/bin/pip check
../clean-venv/bin/ruff check .
../clean-venv/bin/ruff format --check .
```

The candidate-kit path was the extracted supplied archive, not a modified copy. The original seed script inserted six employees and nine attendance records into a generated disposable database before startup created indexes. `python -m compileall -q app tests` also passed (executed with paths to the final repository).

The checked suite includes strict lower/upper epoch boundaries; seconds, floating-point, string, boolean and null rejection; explicit-vs-omitted fields; real dates; shift constraints; second truncation; midnight/month/year/leap transitions; 10-minute grace; 30-minute overtime; 4.125 half-up rounding; rounded 4.50 half-day threshold; R6–R10; legacy omissions; exact response-schema validation; and status/error cases. Reference analytics run across 25 synthetic employees, four months, department filters, rank limits, weekend-only ranges, empty periods and join-date changes. Seeded derived values deliberately differ from possible recomputations to verify that reports read stored metrics.

Concurrency runs use real TCP requests to uvicorn: ten rounds of eight simultaneous requests for each of employee creation, punch-in, punch-out and correction (**320 requests**). Each create/punch-in round had one 201 and seven 409s. Each punch-out round had one 200 and seven 409s. All corrections either committed with a matching audit entry or returned 409; final BSON histories formed a complete before/after chain. A separate barrier at the database read forces two corrections to use the same snapshot: exactly one 200 and one 409.

## Startup and recovery

Fresh clone, fresh virtual environment, installed declared dependencies, exact command:

```sh
uvicorn app.main:app --port 8000
```

Health returned `{"status":"ok"}` in **0.781 seconds**, with all nine secondary indexes created. The 100k benchmark created indexes against a preseeded database and reached readiness in **0.856 seconds**. Two process launches against the same seeded database verified idempotence.

A separate owned MongoDB process was started after the app, stopped, then restarted: initial unavailable 503 in 5.746 s; recovery to 200 in 1.55 s with both unique indexes verified; running outage 503 in 2.518 s; second recovery 200 in 0.604 s. No user database or existing server was stopped.

## Performance and explain

Environment: native MongoDB 7.0.28, Python 3.11.15, macOS 27.0.1 ARM64, localhost HTTP and database. Dataset: **1,000 employees, 100,000 attendance records**, 100 calendar days, ten departments. Seed insertion and index creation occur before timings. Each query has one initial request and five subsequent measurements; the table reports the latter median. These are local observations, not throughput or production latency guarantees. The initial request is not a cold-storage/cache benchmark.

Reproduction:

```sh
TEST_MONGO_URI=mongodb://127.0.0.1:27028 python -m tests.benchmark
```

The script compares every analytical response to independent reference results and verifies list keys, totals and order. It recursively inspects raw explain output and lookup scan counters. All **nine tested query shapes** matched reference results, contained `IXSCAN`, and contained **no `COLLSCAN`**, with **zero lookup collection scans**. No index hints are used. Explain is executed at `executionStats` verbosity.

| Endpoint/query shape | Median HTTP ms | Main access / lookup index |
|---|---:|---|
| Attendance, unfiltered | 13.04 | attendance_list_order |
| Attendance, status/date/page | 1.80 | attendance_status_order |
| Attendance, employee/date | 0.98 | attendance_natural_key_uq |
| Employee monthly | 0.86 | employee_code_uq → attendance_natural_key_uq |
| Department summary, all | 134.29 | joined_on → attendance_natural_key_uq |
| Department summary, filtered | 15.31 | department_joined → attendance_natural_key_uq |
| Late leaderboard, all | 31.90 | attendance_date_late → employee_code_uq |
| Late leaderboard, filtered | 33.06 | attendance_date_late → employee_code_uq |
| Department daily trend | 17.80 | department_code → attendance_natural_key_uq; department_joined |

Unfiltered listing's main page query examined 20 keys and 20 documents; HTTP time also includes its exact total count. Filtered page two examined 40 keys and 20 documents. Employee monthly examined one employee and 31 logs. All-department summary examined 1,000 employees and 31,000 monthly logs. The leaderboard examined 26,567 positive-late log documents and 857 employee join documents before ranking. The revised trend's daily-totals lookup examined 3,131 documents, with daily headcounts examining a further 3,100; these are separate stages, not a de-duplicated global total.

The initial trend implementation took 522.49 ms median and joined logs across all departments. Selecting department employees before their indexed log lookup reduced the final observed median to 17.80 ms on the same dataset. Analytics still matched all independent expectations. Raw explains include rejected candidate plans as well as winning plans; a SORT in a rejected plan is not evidence of a blocking sort in the winning list query. Grouped rankings and moving windows necessarily order their analytical results.

## Risks and limits

- Hidden evaluator data and MongoDB 6.0 were not run. The code uses operators available in 6.0; runtime evidence is for 7.0.28.
- Exact totals, deep offset pagination, many employees, and large history arrays can become costly. Reports are not cross-collection snapshot transactions.
- Duplicate business keys already present in externally corrupted data prevent unique-index initialization; the service reports unready rather than silently accepting unsafe writes.
- One TestClient deprecation warning remains. Both the original and fresh dependency installations passed `pip check`.
- Tests create/delete only fresh UUID-named databases. No seeded evaluator, production or user-selected database is dropped.
- Form submission is separate from code verification and requires the applicant's exact email and approval.
