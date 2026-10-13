"""Final contract audit: uncommon legal pagination and invalid numeric bodies."""

import json
import random
from datetime import date, timedelta

import pytest

from tests.test_analytics import (
    leaderboard_reference,
    monthly_reference,
    summary_reference,
    trend_reference,
)
from tests.test_contract import check, employee, ms


@pytest.mark.parametrize("path", ["/employees", "/attendance", "/admin/explain/attendance_list"])
@pytest.mark.parametrize("page", [2**63, 10**100])
def test_page_beyond_bson_integer_range(api, path, page):
    http, db = api
    assert http.post("/employees", json=employee()).status_code == 201
    assert (
        http.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP1001", "punched_at": ms("2026-07-06T09:30:00")},
        ).status_code
        == 201
    )
    response = http.get(path, params={"page": page, "page_size": 100})
    assert response.status_code == 200, response.text
    if path.startswith("/admin"):
        assert response.json()["explain"]["executionStats"]["nReturned"] == 0
    else:
        assert response.json() == {"items": [], "total": 1, "page": page, "page_size": 100}


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("POST", "/attendance/punch-in", '{"emp_code":"EMP1001","punched_at":1e400}'),
        ("POST", "/attendance/punch-out", '{"emp_code":"EMP1001","punched_at":-1e400}'),
        (
            "PATCH",
            "/attendance/EMP1001/2026-07-06",
            '{"reason":"fix clock","regularized_by":"hr","punch_out":1e400}',
        ),
        (
            "POST",
            "/employees",
            '{"emp_code":"EMP1001","name":1e400,"email":"x@example.org","department":"D","joined_on":"2026-01-01"}',
        ),
        (
            "POST",
            "/attendance/punch-in",
            '{"emp_code":"EMP1001","punched_at":null,"unused":[1e400]}',
        ),
    ],
)
def test_nonfinite_numeric_validation_is_422(api, method, path, body):
    response = api[0].request(
        method, path, content=body, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422, response.text
    assert isinstance(response.json()["detail"], list)
    json.dumps(response.json(), allow_nan=False)


@pytest.mark.parametrize(
    "clock,expected",
    [
        ("23:59:59", "2026-12-31"),
        ("00:00:00", "2026-12-31"),
        ("05:59:59", "2026-12-31"),
        ("06:00:00", "2027-01-01"),
        ("06:00:01", "2027-01-01"),
    ],
)
def test_overnight_exact_boundaries(api, clock, expected):
    http, db = api
    assert (
        http.post("/employees", json=employee(shift_start="22:00", shift_end="06:00")).status_code
        == 201
    )
    day = "2026-12-31" if clock == "23:59:59" else "2027-01-01"
    value = ms(day + "T" + clock) + 999
    response = http.post("/attendance/punch-in", json={"emp_code": "EMP1001", "punched_at": value})
    assert response.status_code == 201
    assert response.json()["date"] == expected
    assert int(db.attendance_logs.find_one()["punch_in"].timestamp() * 1000) == value // 1000 * 1000


def test_latest_eligible_record_not_arbitrary_open_record(api):
    http, db = api
    assert http.post("/employees", json=employee()).status_code == 201
    for day in ("2026-07-06", "2026-07-07"):
        assert (
            http.post(
                "/attendance/punch-in",
                json={"emp_code": "EMP1001", "punched_at": ms(day + "T09:30:00")},
            ).status_code
            == 201
        )
    assert (
        http.post(
            "/attendance/punch-out",
            json={"emp_code": "EMP1001", "punched_at": ms("2026-07-06T09:29:59")},
        ).status_code
        == 404
    )
    # Closing a prior eligible shift must ignore the later record.
    assert (
        http.post(
            "/attendance/punch-out",
            json={"emp_code": "EMP1001", "punched_at": ms("2026-07-06T18:30:00")},
        ).status_code
        == 200
    )
    assert db.attendance_logs.find_one({"date": "2026-07-07"})["punch_out"] is None
    assert (
        http.post(
            "/attendance/punch-out",
            json={"emp_code": "EMP1001", "punched_at": ms("2026-07-06T19:00:00")},
        ).status_code
        == 409
    )
    assert (
        http.post(
            "/attendance/punch-out",
            json={"emp_code": "EMP1001", "punched_at": ms("2026-07-07T18:30:00")},
        ).status_code
        == 200
    )


def test_correction_state_transitions_and_rejections_preserve_bson(api):
    http, db = api
    assert http.post("/employees", json=employee()).status_code == 201
    assert (
        http.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP1001", "punched_at": ms("2026-07-06T10:00:00")},
        ).status_code
        == 201
    )
    base = {"reason": "audit correction", "regularized_by": "hr"}
    states = [
        {"status": "ABSENT"},
        {"status": "PRESENT", "punch_in": ms("2026-07-06T09:30:00")},
        {"status": "LEAVE"},
        {"status": "WFH", "punch_in": ms("2026-07-06T09:30:00")},
        {"punch_out": ms("2026-07-06T13:59:42")},
        {"status": "ON_DUTY"},
    ]
    previous = []
    for count, change in enumerate(states, 1):
        response = http.patch("/attendance/EMP1001/2026-07-06", json=base | change)
        assert response.status_code == 200, response.text
        actual = response.json()
        check(actual, "AttendanceRecord")
        assert len(actual["history"]) == count and actual["history"][:-1] == previous
        previous = actual["history"]
    assert actual["work_hours"] == 4.50 and actual["half_day"] is False
    old = db.attendance_logs.find_one()
    for change in (
        {},
        {"punch_in": ms("2026-07-07T09:30:00")},
        {"punch_out": ms("2026-07-07T09:30:01")},
        {"status": "ABSENT", "punch_out": ms("2026-07-06T19:00:00")},
    ):
        assert http.patch("/attendance/EMP1001/2026-07-06", json=base | change).status_code == 422
        assert db.attendance_logs.find_one() == old


def test_employee_pagination_ends_and_case(api):
    http, _ = api
    for n in (3, 1, 2):
        assert (
            http.post("/employees", json=employee(f"EMP{n:04}", department="Sales")).status_code
            == 201
        )
    for page, codes in [(1, ["EMP0001", "EMP0002"]), (2, ["EMP0003"]), (3, [])]:
        response = http.get(
            "/employees", params={"department": "Sales", "page": page, "page_size": 2}
        ).json()
        assert response["total"] == 3 and [r["emp_code"] for r in response["items"]] == codes
    assert http.get("/employees", params={"department": "sales"}).json()["total"] == 0


@pytest.mark.parametrize("seed", [19, 2026, 8317])
def test_seeded_cross_year_analytics_oracle(api, seed):
    http, db = api
    rng = random.Random(seed)
    staff = [
        employee(
            f"EMP{i:04}",
            department=(
                "NoLogs" if i == 8 else "EmptyUntilFebruary" if i == 9 else "D" + str(i % 2)
            ),
            joined_on=(
                "2027-02-01"
                if i == 9
                else "2027-01-31"
                if i == 7
                else "2027-01-04"
                if i % 3 == 0
                else "2026-12-01"
            ),
        )
        for i in range(1, 10)
    ]
    rows = []
    for emp in staff:
        if emp["department"] in ("NoLogs", "EmptyUntilFebruary"):
            continue
        for offset in range(40):
            day = date(2026, 12, 20) + timedelta(days=offset)
            if rng.random() < 0.35 or str(day) < emp["joined_on"]:
                continue
            row = {
                "emp_code": emp["emp_code"],
                "date": str(day),
                "status": rng.choice(["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]),
                "late_minutes": rng.choice([0, 0, 10, 30, 60]),
                "overtime_minutes": rng.choice([0, 30, 60]),
                "half_day": rng.choice([True, False]),
                "work_hours": rng.choice([None, 4.49, 4.50, 8.12, 8.13]),
            }
            if rng.random() < 0.2:
                for field in ("late_minutes", "overtime_minutes", "half_day"):
                    row.pop(field)
            rows.append(row)
    rows.append(
        {"emp_code": "orphan", "date": "2027-01-01", "status": "PRESENT", "late_minutes": 9999}
    )
    db.employees.insert_many([dict(e) for e in staff])
    db.attendance_logs.insert_many([dict(r) for r in rows])
    for month in ("2026-12", "2027-01"):
        for emp in staff:
            assert http.get(
                f"/analytics/employees/{emp['emp_code']}/monthly", params={"month": month}
            ).json() == monthly_reference(emp, rows, month)
        for dep in (None, "D0", "D1", "NoLogs", "EmptyUntilFebruary"):
            params = {"month": month} | ({"department": dep} if dep else {})
            assert http.get(
                "/analytics/departments/summary", params=params
            ).json() == summary_reference(staff, rows, month, dep)
            assert http.get(
                "/analytics/leaderboard/late", params=params | {"limit": 2}
            ).json() == leaderboard_reference(staff, rows, month, 2, dep)
    for dep in ("D0", "D1", "NoLogs", "EmptyUntilFebruary"):
        for first, last in [
            ("2026-12-26", "2027-01-10"),
            ("2027-01-04", "2027-01-04"),
            ("2027-01-30", "2027-02-05"),
        ]:
            assert http.get(
                f"/analytics/departments/{dep}/trend", params={"from": first, "to": last}
            ).json() == trend_reference(staff, rows, dep, first, last)


@pytest.mark.parametrize(
    "path,params",
    [
        ("attendance_list", {"month": "bad"}),
        ("attendance_list", {"month": "2026-13"}),
        ("employee_monthly", {"date_from": "bad"}),
        ("employee_monthly", {"date_to": "2026-02-30"}),
        ("employee_monthly", {"from": "2026-1-1"}),
        ("employee_monthly", {"to": ""}),
    ],
)
def test_explain_validates_supplied_calendar_parameters(api, path, params):
    http, _ = api
    query = {"month": "2026-07", "emp_code": "EMP1001"} | params
    assert http.get("/admin/explain/" + path, params=query).status_code == 422
