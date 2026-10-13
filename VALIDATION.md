# Validation evidence

Executed 14 October 2026 on Python 3.11.15, macOS 27.0.1 ARM64, native MongoDB **6.0.29 and 7.0.28**. This records observed results, not hidden-grader results or deployment guarantees.

## Current verification

| Check | MongoDB 7.0.28 | MongoDB 6.0.29 |
|---|---|---|
| Complete pytest suite, original sample included | 115 passed; 0 failed/skipped | 115 passed; 0 failed/skipped |
| 100k benchmark, all five explain modes | 19 shapes passed | 19 shapes passed |
| Winning/executed plans | IXSCAN; no COLLSCAN; zero reported lookup collection scans | IXSCAN; no COLLSCAN; zero reported lookup collection scans |

Ruff checks, formatting, compilation and `pip check` passed. One nonfailing Starlette TestClient/httpx deprecation warning remains. All application code, requirements, indexes and pipelines are byte-identical to the previously audited `0808500`; this quality pass changes documentation and test tooling only.

From the repository root, reproduce the complete suite against each available server:

```sh
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
export CANDIDATE_KIT=/absolute/path/to/candidate_kit
TEST_MONGO_URI=mongodb://127.0.0.1:27028 python -m pytest -q
TEST_MONGO_URI=mongodb://127.0.0.1:27026 python -m pytest -q
TEST_MONGO_URI=mongodb://127.0.0.1:27028 python -m tests.benchmark
TEST_MONGO_URI=mongodb://127.0.0.1:27026 python -m tests.benchmark
ruff check .
ruff format --check .
python -m compileall -q app tests
pip check
```

Ports above identify the isolated servers used here; use your development MongoDB URI. Tests create fresh `hrone_test_<UUID>` databases and drop only those generated names. They never drop `MONGO_DB` or a supplied database name. Without `TEST_MONGO_URI`, database tests skip explicitly; without `CANDIDATE_KIT`, the original-sample test skips. The candidate kit is the unchanged supplied archive; its seed script runs only against a disposable database. Production installation requires only `requirements.txt`.

## Contract and concurrency coverage

All 12 operations are exercised: readiness; employee create/list; attendance punch-in, punch-out, list and correction; employee monthly, department summary, leaderboard and trend; and explain's five modes. Tests validate response schemas, status codes, persisted BSON and nested history.

Boundary coverage includes strict bounded integer epochs, omitted versus null fields, seconds truncation, IST midnight/overnight assignment, leap/month/year transitions, December 9999, 600/601-second grace, overtime thresholds, 24-hour durations, half-up ties and the rounded 4.50-hour half-day rule. Analytics compare complete responses to independent test-only references, including no-log employees, joiners, legacy omissions, orphans, record-weighted averages, competition ties and 92-day trends. Cross-year randomized comparisons record seeds 19, 2026 and 8317.

Real TCP races run ten rounds of eight simultaneous requests for each of employee creation, punch-in, punch-out and correction (320 requests). Duplicate creates/punch-ins admit one 201; punch-outs admit one 200. Correction histories retain every committed change and derived fields match the final times. A separate same-snapshot correction test admits one 200 and one 409.

Three new deterministic interleavings hold a request after its read while the other endpoint commits: punch-out versus correction, correction versus punch-out, and punch-out versus two corrections restoring the original punch time. The stale request returns 409 without changing committed BSON; retry succeeds with correct derived fields and complete history. The last case specifically protects against relying only on equal times/status when history has changed.

## Reproducible benchmark

`python -m tests.benchmark` now includes the same **19 shapes previously checked by external audit tooling**. It seeds 1,000 employees and 100,000 attendance records across 100 dates and ten departments. Each analytical response is compared in full with an independent reference; listing verifies keys, ordering and filtered total. Additional shapes cover one-day, status-only, combined employee/status/date, page 4,000, empty periods and a 92-day trend.

The script prints JSON containing parameters, server/Python versions, first-request and five subsequent request timings, winning plans, executed stage trees, indexes and per-stage examined/returned counts. It excludes `rejectedPlans` and `allPlansExecution` from used-plan analysis, then checks main stages and lookup scan counters. This avoids mistaking an index in a rejected alternative for actual indexed execution. Some nested lookups expose counters/indexes rather than a complete inner tree. Integration tests separately assert explain/list command and aggregation-builder equivalence with production. No hints are used.

Median localhost HTTP milliseconds from this quality pass (five calls after one initial call; insertion/index creation excluded):

| Query | MongoDB 7.0.28 ms | MongoDB 6.0.29 ms | Winning / lookup indexes |
|---|---:|---:|---|
| Attendance, unfiltered | 12.33 | 13.66 | attendance_list_order |
| Attendance, status/date/page | 1.74 | 1.80 | attendance_status_order |
| Attendance, employee/date | 0.97 | 1.03 | attendance_natural_key_uq |
| Employee monthly | 0.94 | 1.29 | employee_code_uq → attendance_natural_key_uq |
| Department summary, all | 131.41 | 135.24 | joined_on → attendance_natural_key_uq |
| Department summary, filtered | 15.06 | 15.61 | department_joined → attendance_natural_key_uq |
| Late leaderboard, all | 32.46 | 36.07 | attendance_date_late → employee_code_uq |
| Late leaderboard, filtered | 32.29 | 35.88 | attendance_date_late → employee_code_uq |
| Department trend, 31 days | 17.97 | 25.51 | department_code / department_joined → attendance_natural_key_uq |

Deep page 4,000: **41.05/43.20 ms** (7.0/6.0); 92-day trend: **33.61/42.14 ms**. Index-building readiness on the preseeded fixture: **0.796/1.281 s**. These are sequential local samples, not throughput guarantees or cold-storage measurements. Per-stage counts must not be summed across enclosing/nested counters without accounting for overlap. Exact list totals are a separate query included in HTTP latency, not the explained main page query.

For comparison, the prior 7.0 audit measured 14.28 ms listing, 1.70 ms monthly, 134.01 ms summary, 33.37 ms leaderboard and 18.33 ms trend on this fixture. Application/query bytes are unchanged; timing differences reflect repeated-run conditions rather than an implemented performance optimization.

## Previously executed release checks

The preceding audit installed declared production dependencies in a fresh Python 3.11.15 virtual environment and ran the exact command `uvicorn app.main:app --port 8000`: readiness in **0.771 s** without `.env`, then **0.273 s** with a deliberately conflicting `.env`. Supplied environment variables selected the correct database; all nine secondary indexes and restart idempotence were verified. Development dependencies were then installed for complete tests on both database versions. These checks apply to the unchanged application/dependency bytes; they were not new startup measurements in this quality pass.

A separately owned server outage experiment returned initial 503 in **5.755 s**, recovered to 200 in **0.644 s**, returned running-outage 503 in **2.992 s**, and recovered again in **0.600 s**. Unique indexes were verified after recovery. No user server was stopped. Docker's daemon was unavailable; official native standalone binaries were used. No replica set is required by the implementation.

## Audit provenance and limits

- `07a0fad`: the unchanged 84-test baseline passed on both database versions. `0808500`: 112 tests/version, an independently parsed original-OpenAPI matrix with 151 cases/version, and 19 externally executed benchmark shapes/version passed. The 151-case matrix was supplemental audit tooling outside this repository; the committed pytest suite is a separate reproducible check. The current benchmark closes the prior nine-versus-19 reproducibility gap.
- The previous audit fixed three reproduced edge defects: signed-64-bit pagination overflow, nonfinite echoed validation inputs causing HTTP 500, and explain mode validation of unused supplied calendar values. Focused regressions remain in `test_adversarial.py`.
- Hidden evaluator data and other database patch versions, hosted/sharded systems and Linux deployment were not executed. MongoDB 6.0 compatibility is supported by actual 6.0.29 runs, including all analytics, windows and explain modes.
- Exact totals, deep offsets and long history arrays retain scaling costs. Reports are not cross-collection snapshot transactions. Externally corrupted duplicate business keys prevent unique-index initialization and readiness; no destructive repair is attempted.
- Git checks exclude secrets, `.env`, Dockerfile, virtual environments, caches, datasets and temporary benchmark output from tracked files.

## Earlier implementation findings (historical)

- `month_bounds("9999-12")` raised `OverflowError`; a new HTTP regression reproduced it. Month filters now use the inclusive last day, and MongoDB computes the next-month boundary for weekday counting. Valid December 9999 returns normally.
- Startup swallowed database connection failure without retrying index initialization. On later recovery, a successful ping could mark the app ready without unique indexes. `ensure_indexes` now retries under a process-local initialization lock before writes/readiness; database indexes remain the cross-process concurrency authority. A controlled startup failure and a real stop/restart test verify recovery.
- Business-rule 422 responses used string details, while the supplied validation schema describes an array. `invalid` now produces a validation-error array, covered by HTTP tests.
- Punch-out checked times/status but not history. Its conditional update now also compares the observed history, detecting intervening corrections even if values cycle back to the original state.
- Trend joined every department's logs before filtering. The measured 100k run justified starting its inner lookup with department employees, then using the attendance natural-key index. Independent reference results remain identical.
- Existing integration cleanup accepted a caller-selected database name and only compared it with `MONGO_DB`. Tests now own fresh UUID-named databases, reject existing names, and never drop supplied database names.
- Explain/list shared only the filter previously. Both now derive their complete filter, sort, projection, offset and limit from `attendance_command`.
- The earlier decisions document said employees joined “within the month”; the implemented and contractual condition is “on or before month-end.” Corrected the explanation.
