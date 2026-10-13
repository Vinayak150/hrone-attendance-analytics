# Design decisions

1. **Indexes.** Unique `emp_code` and `(emp_code,date)` indexes protect employees and daily attendance; the latter also supports report lookups. `(date DESC,emp_code)` and `(status,date DESC,emp_code)` support ordered attendance pages. `(emp_code,punch_in DESC)` finds the latest punch. Employee `(department,emp_code)`, `(department,joined_on)`, and `joined_on` indexes support listing, department selection, and headcounts. Attendance `(date,late_minutes)` supports monthly late filtering. I rejected `history.at`: no endpoint searches audit timestamps.

2. **Punch-in race.** Both requests can read the employee and calculate the same attendance date. Both attempt insertion. MongoDB's unique compound index admits one document; the losing insert raises `DuplicateKeyError`, translated to 409. The winner returns 201. No preliminary attendance lookup decides uniqueness.

3. **Ties.** `$rank` orders only total late minutes. Filtering `rank <= limit` afterward preserves all cutoff ties, even when more rows than the limit return. A final employee-code sort orders ties without changing ranks.

4. **Headcount.** The summary starts with employees who joined on or before month-end. Each contributes one headcount. A left lookup aggregates their logs; an empty result contributes zero statistics and no work-hours denominator.

5. **100x growth.** Introduce reconciled daily rollups, updating affected employee/date totals after corrections. Preserve source records and verify rollups against the current pipelines.
