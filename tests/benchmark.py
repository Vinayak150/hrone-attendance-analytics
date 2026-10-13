"""Run TEST_MONGO_URI=... python -m tests.benchmark; owns a random disposable database."""

import json
import platform
import statistics
import time
from datetime import date, datetime, timedelta, timezone

from tests.conftest import disposable_database
from tests.test_analytics import (
    leaderboard_reference,
    monthly_reference,
    summary_reference,
    trend_reference,
)
from tests.test_concurrency import live
from tests.test_contract import employee
from tests.test_integration import walk


def main():
    with disposable_database() as db:
        staff = [
            employee(
                f"EMP{i:06}",
                department=f"Department{i % 10}",
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                shift_start="09:30",
                shift_end="18:30",
            )
            for i in range(1000)
        ]
        db.employees.insert_many([dict(e) for e in staff])
        logs = []
        for n in range(100):
            day = date(2026, 5, 1) + timedelta(days=n)
            for i, emp in enumerate(staff):
                status = ["PRESENT", "WFH", "ON_DUTY", "LEAVE", "ABSENT"][i % 5]
                logs.append(
                    dict(
                        emp_code=emp["emp_code"],
                        date=str(day),
                        status=status,
                        punch_in=datetime.combine(day, datetime.min.time(), timezone.utc)
                        + timedelta(hours=4),
                        punch_out=None,
                        work_hours=8.125 if i % 5 < 3 else None,
                        late_minutes=(i % 7) * 10,
                        overtime_minutes=30 if i % 3 == 0 else 0,
                        half_day=i % 11 == 0,
                    )
                )
            if len(logs) % 10000 == 0:
                db.attendance_logs.insert_many([dict(r) for r in logs[-10000:]])
        assert db.attendance_logs.count_documents({}) == 100000
        gen = live.__wrapped__(db)
        start = time.perf_counter()
        try:
            http, _ = next(gen)
            startup_ms = round((time.perf_counter() - start) * 1000, 2)
            cases = [
                ("attendance_list", "/attendance", {}),
                (
                    "attendance_list",
                    "/attendance",
                    {
                        "status": "WFH",
                        "date_from": "2026-07-01",
                        "date_to": "2026-07-31",
                        "page": 2,
                        "page_size": 20,
                    },
                ),
                (
                    "attendance_list",
                    "/attendance",
                    {"emp_code": "EMP000001", "date_from": "2026-07-01", "date_to": "2026-07-31"},
                ),
                (
                    "employee_monthly",
                    "/analytics/employees/EMP000001/monthly",
                    {"month": "2026-07"},
                ),
                ("department_summary", "/analytics/departments/summary", {"month": "2026-07"}),
                (
                    "department_summary",
                    "/analytics/departments/summary",
                    {"month": "2026-07", "department": "Department1"},
                ),
                (
                    "late_leaderboard",
                    "/analytics/leaderboard/late",
                    {"month": "2026-07", "limit": 2},
                ),
                (
                    "late_leaderboard",
                    "/analytics/leaderboard/late",
                    {"month": "2026-07", "limit": 2, "department": "Department1"},
                ),
                (
                    "department_trend",
                    "/analytics/departments/Department1/trend",
                    {"from": "2026-07-01", "to": "2026-07-31"},
                ),
            ]
            results = []
            for mode, path, params in cases:
                latencies = []
                for _ in range(6):
                    before = time.perf_counter()
                    response = http.get(path, params=params)
                    latencies.append((time.perf_counter() - before) * 1000)
                    assert response.status_code == 200, response.text
                actual = response.json()
                if mode == "employee_monthly":
                    expected = monthly_reference(staff[1], logs, "2026-07")
                elif mode == "department_summary":
                    expected = summary_reference(staff, logs, "2026-07", params.get("department"))
                elif mode == "late_leaderboard":
                    expected = leaderboard_reference(
                        staff, logs, "2026-07", 2, params.get("department")
                    )
                elif mode == "department_trend":
                    expected = trend_reference(
                        staff, logs, "Department1", params["from"], params["to"]
                    )
                else:
                    filtered = [
                        r
                        for r in logs
                        if all(r[k] == params[k] for k in ("emp_code", "status") if k in params)
                        and r["date"] >= params.get("date_from", "")
                        and r["date"] <= params.get("date_to", "9999-12-31")
                    ]
                    filtered.sort(
                        key=lambda r: (-date.fromisoformat(r["date"]).toordinal(), r["emp_code"])
                    )
                    skip = (params.get("page", 1) - 1) * params.get("page_size", 20)
                    assert actual["total"] == len(filtered)
                    assert [(r["emp_code"], r["date"]) for r in actual["items"]] == [
                        (r["emp_code"], r["date"])
                        for r in filtered[skip : skip + params.get("page_size", 20)]
                    ]
                    expected = actual
                assert actual == expected, mode
                ep = params | (
                    {"emp_code": "EMP000001"}
                    if mode == "employee_monthly"
                    else {"department": "Department1"}
                    if mode == "department_trend"
                    else {}
                )
                response = http.get("/admin/explain/" + mode, params=ep)
                assert response.status_code == 200, response.text
                plan = response.json()["explain"]
                nodes = list(walk(plan))
                stages = sorted({n["stage"] for n in nodes if "stage" in n})
                indexes = sorted(
                    {n["indexName"] for n in nodes if "indexName" in n}
                    | {i for n in nodes for i in n.get("indexesUsed", [])}
                )
                scans = sum(n.get("collectionScans", 0) for n in nodes)
                assert "IXSCAN" in stages and "COLLSCAN" not in stages and scans == 0, (
                    mode,
                    stages,
                    scans,
                )
                stats = [
                    {
                        k: n[k]
                        for k in (
                            "stage",
                            "nReturned",
                            "totalKeysExamined",
                            "totalDocsExamined",
                            "executionTimeMillis",
                            "executionTimeMillisEstimate",
                            "indexesUsed",
                            "collectionScans",
                        )
                        if k in n
                    }
                    for n in nodes
                    if "totalDocsExamined" in n
                ]
                results.append(
                    dict(
                        endpoint=mode,
                        params=params,
                        cold_ms=round(latencies[0], 2),
                        warm_median_ms=round(statistics.median(latencies[1:]), 2),
                        warm_min_ms=round(min(latencies[1:]), 2),
                        warm_max_ms=round(max(latencies[1:]), 2),
                        stages=stages,
                        indexes=indexes,
                        collection_scans=scans,
                        execution_sections=stats,
                        reference_match=True,
                    )
                )
            print(
                json.dumps(
                    dict(
                        mongodb=db.client.server_info()["version"],
                        python=platform.python_version(),
                        platform=platform.platform(),
                        employees=len(staff),
                        attendance=100000,
                        startup_index_build_ms=startup_ms,
                        results=results,
                    ),
                    indent=2,
                )
            )
        finally:
            try:
                next(gen)
            except StopIteration:
                pass


if __name__ == "__main__":
    main()
