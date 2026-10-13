# Starter code review

Each item is from the supplied `app/main.py`, before the rewrite. The table records a reproducible failure and the corresponding correction.

| # | Where | Defect | How to expose it | Correction |
|---|---|---|---|---|
| 1 | `health` | Returns success without checking MongoDB. | Stop MongoDB; `/health` still returns 200. | Ping MongoDB; return 503 if unreachable. |
| 2 | `create_employee` | Checks for duplicates before inserting; not atomic. | Parallel POSTs with the same code can both pass the read. | Unique `emp_code` index; catch `DuplicateKeyError` as 409. |
| 3 | `EmployeeIn` | Does not validate business-code, email, lengths, actual join date, or shift format. | `EMPx`, `bad-email`, or `2026-02-30` is accepted. | Match the OpenAPI constraints and validate calendar dates. |
| 4 | `create_employee` | Allows equal shift start and end. | Create an employee with `09:30` / `09:30`. | Reject with 422. |
| 5 | `create_employee` | Stores naive local `created_at`. | Run in a non-UTC system timezone; stored instant is ambiguous. | Use aware UTC server time, convert to epoch ms on output. |
| 6 | `create_employee` / `list_employees` | Returns BSON datetimes without the required epoch-ms conversion. | Inspect `created_at` JSON: expected an integer. | Centralize employee serialization. |
| 7 | `list_employees` | Uses `page * page_size` instead of `(page-1) * page_size`. | Page 1 starts at the 21st employee. | Correct offset formula. |
| 8 | `list_employees` | Counts all employees regardless of the department filter. | Filter Sales, compare `total` with returned Sales records. | Count using the identical filter. |
| 9 | `list_employees` | No explicit `emp_code` sort. | Insert employees out of order and request page 1. | Indexed ascending sort before pagination. |
| 10 | List endpoints | Missing page and page-size constraints. | `page=0`, `page_size=500`, or negative values are accepted. | Validate `page>=1` and `1<=page_size<=100`. |
| 11 | `punch_in` | Dereferences missing employee without a 404. | Unknown code triggers `emp["shift_start"]` on `None`. | Check existence before processing. |
| 12 | `PunchInIn` | Does not enforce allowed statuses or strict epoch milliseconds. | Send `ABSENT`, a float, a string, or a seconds timestamp. | Restrict to presence statuses and strict bounded integers. |
| 13 | `punch_in` | Uses truthiness and host-local naive time conversions. | Missing timestamp, different server timezones or fractional milliseconds. | Use aware UTC and explicit `is not None`; truncate to seconds. |
| 14 | `punch_in` | Uses server-local calendar date, not IST attendance date. | Punch at 00:30 IST or 04:00 IST on an overnight shift. | Derive the IST day, including previous-day overnight rule. |
| 15 | `punch_in` | Uses check-then-insert without a unique attendance index. | Concurrent same-day punch-ins can create duplicates. | Unique `(emp_code,date)` index and error translation. |
| 16 | `compute_late_minutes` | Uses naive replacement on the punch date and truncates elapsed minutes **before** checking 10-minute grace. | 09:40:01 incorrectly returns zero; with UTC input 04:35 for a 09:30 IST shift, it reports zero rather than 35. Overnight dates also need the previous shift start. | Compare full elapsed seconds against 600, then floor full minutes relative to shift start. |
| 17 | `compute_work_hours` | Python `round` uses half-even, not required half-up. | Duration of exactly 4.125 hours must round to 4.13, not 4.12. | Use `Decimal.quantize(ROUND_HALF_UP)` on exact seconds. |
| 18 | `compute_overtime` | Counts 1–29 overtime minutes and mishandles overnight shifts. | 18:45 for an 18:30 shift returns 15 instead of 0; overnight end is the next day. | Apply 30-minute threshold using actual shift end date. |
| 19 | `punch_in` | Adds an `id` field not present in the API. | POST response contains `id` despite natural-key addressing. | Omit `_id` and `id`; return only the contracted record. |
| 20 | `list_attendance` | Pulls all rows into Python before sorting and paginating. | 100,000 logs produce large memory use and linear transfer. | Filter, index-sort, skip and limit in MongoDB. |
| 21 | `list_attendance` | Sorts only by date; misses `emp_code` tie order. | Two employees on the same day come back in arbitrary order. | Sort by `(date DESC, emp_code ASC)`. |
| 22 | `list_attendance` | Exposes a synthetic `id`, and BSON values are not converted to epoch ms, including history timestamps. | Response shape disagrees with the contract and history may serialize incorrectly. | Use a uniform serializer for records and nested changes. |
| 23 | `list_attendance` | Fails to reject reversed date ranges or non-calendar dates; status is unrestricted. | `date_from=2026-07-31&date_to=2026-07-01` returns 200. | Shared date and enum validation, returning 422. |
| 24 | `list_attendance` | Does not supply defaults for legacy missing `history` and `half_day`. | A seeded legacy row fails the response schema. | Normalize missing `history=[]`, `half_day=false` and other optional stored values. |

| 25 | Module initialization | No startup indexes are created. The seeded database has only `_id`, so uniqueness and query access paths are not established. | Start against the original sample database and inspect `index_information()`: no business-key or report indexes. | `ensure_indexes` creates all indexes idempotently before readiness/writes. |

## Examined and retained

- The starter's `load_dotenv()` call is not a defect: its default `override=False` means real environment variables win. This behavior is retained.
- A PyMongo-backed synchronous FastAPI route is acceptable here because FastAPI runs `def` handlers in a worker thread. The service uses atomic database writes for shared mutable state instead of Python locks.
- No client-authentication layer is necessary for `regularized_by`; the contract explicitly treats it as free text.

## Additional findings in the earlier submission

- `month_bounds("9999-12")` raised `OverflowError`; a new HTTP regression reproduced it. Month filters now use the inclusive last day, and MongoDB computes the next-month boundary for weekday counting. Valid December 9999 returns normally.
- Startup swallowed database connection failure without retrying index initialization. On later recovery, a successful ping could mark the app ready without unique indexes. `ensure_indexes` now retries under a process-local initialization lock before writes/readiness; database indexes remain the cross-process concurrency authority. A controlled startup failure and a real stop/restart test verify recovery.
- Business-rule 422 responses used string details, while the supplied validation schema describes an array. `invalid` now produces a validation-error array, covered by HTTP tests.
- Punch-out checked times/status but not history. Its conditional update now also compares the observed history, detecting intervening corrections even if values cycle back to the original state.
- Trend joined every department's logs before filtering. The measured 100k run justified starting its inner lookup with department employees, then using the attendance natural-key index. Independent reference results remain identical.
- Existing integration cleanup accepted a caller-selected database name and only compared it with `MONGO_DB`. Tests now own fresh UUID-named databases, reject existing names, and never drop supplied database names.
- Explain/list shared only the filter previously. Both now derive their complete filter, sort, projection, offset and limit from `attendance_command`.
- The earlier decisions document said employees joined “within the month”; the implemented and contractual condition is “on or before month-end.” Corrected the explanation.
