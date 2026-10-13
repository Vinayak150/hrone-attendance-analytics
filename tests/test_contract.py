import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

SCHEMAS = json.loads(Path(__file__).with_name("response_schemas.json").read_text())


def check(value, name):
    Draft202012Validator(
        {**SCHEMAS, "$ref": "#/$defs/" + name}, format_checker=FormatChecker()
    ).validate(value)


def employee(code="EMP1001", **extra):
    return (
        dict(
            emp_code=code,
            name="Test",
            email="test@example.org",
            department="Engineering",
            joined_on="2026-01-01",
        )
        | extra
    )


def ms(value):
    return int(datetime.fromisoformat(value + "+05:30").timestamp() * 1000)


@pytest.mark.parametrize("month", ["0000-01", "bad", "2026-13"])
def test_bad_month_api(api, month):
    assert api[0].get("/analytics/departments/summary", params={"month": month}).status_code == 422


def test_calendar_upper_month(api):
    http, _ = api
    assert http.post("/employees", json=employee()).status_code == 201
    r = http.get("/analytics/employees/EMP1001/monthly", params={"month": "9999-12"})
    assert r.status_code == 200, r.text
    assert r.json()["working_days"] == 23


@pytest.mark.parametrize(
    "field,value",
    [
        ("emp_code", "EMP1"),
        ("name", ""),
        ("name", "a" * 101),
        ("email", "invalid"),
        ("department", ""),
        ("department", "x" * 51),
        ("shift_start", "24:00"),
        ("shift_end", "9:30"),
        ("joined_on", "2026-02-30"),
        ("joined_on", "20260701"),
    ],
)
def test_employee_validation_http(api, field, value):
    assert api[0].post("/employees", json=employee(**{field: value})).status_code == 422


@pytest.mark.parametrize(
    "value", [99999999999, 4102444800001, 1783312500, "1783312500000", 1783312500000.0, True, None]
)
@pytest.mark.parametrize("route", ["punch-in", "punch-out", "patch"])
def test_strict_timestamp_http(api, route, value):
    http, _ = api
    if route == "patch":
        r = http.patch(
            "/attendance/EMP1001/2026-07-06",
            json={"reason": "valid reason", "regularized_by": "hr", "punch_in": value},
        )
    else:
        r = http.post("/attendance/" + route, json={"emp_code": "EMP1001", "punched_at": value})
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], list)


def test_full_workflow_schema_errors_and_bson(api):
    http, db = api
    r = http.post("/employees", json=employee(created_at=123))
    assert r.status_code == 201
    check(r.json(), "Employee")
    assert isinstance(db.employees.find_one()["created_at"], datetime)
    assert http.post("/employees", json=employee()).status_code == 409
    for code in ("EMP1003", "EMP1002"):
        assert http.post("/employees", json=employee(code, department="Sales")).status_code == 201
    page = http.get("/employees", params={"department": "Sales", "page_size": 1, "page": 2}).json()
    check(page, "EmployeePage")
    assert page["total"] == 2 and page["items"][0]["emp_code"] == "EMP1003"
    assert http.get("/employees", params={"department": "sales"}).json()["total"] == 0
    for path in ("/employees", "/attendance"):
        for params in ({"page": 0}, {"page_size": 0}, {"page_size": 101}):
            assert http.get(path, params=params).status_code == 422
    for route in ("punch-in", "punch-out"):
        assert http.post("/attendance/" + route, json={"emp_code": "missing"}).status_code == 404
    assert (
        http.post(
            "/attendance/punch-out",
            json={"emp_code": "EMP1001", "punched_at": ms("2026-07-06T09:30:00")},
        ).status_code
        == 404
    )
    body = {
        "emp_code": "EMP1001",
        "punched_at": ms("2026-07-06T09:40:00") + 999,
        "status": "WFH",
        "late_minutes": 999,
    }
    r = http.post("/attendance/punch-in", json=body)
    assert r.status_code == 201
    check(r.json(), "AttendanceRecord")
    assert r.json()["late_minutes"] == 0 and r.json()["punch_in"] % 1000 == 0
    assert http.post("/attendance/punch-in", json=body).status_code == 409
    assert (
        http.post(
            "/attendance/punch-out",
            json={"emp_code": "EMP1001", "punched_at": r.json()["punch_in"]},
        ).status_code
        == 422
    )
    assert (
        http.post(
            "/attendance/punch-out",
            json={"emp_code": "EMP1001", "punched_at": r.json()["punch_in"] + 86401000},
        ).status_code
        == 422
    )
    r = http.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP1001", "punched_at": ms("2026-07-06T19:00:59")},
    )
    assert r.status_code == 200
    check(r.json(), "AttendanceRecord")
    assert r.json()["overtime_minutes"] == 30 and r.json()["history"] == []
    assert (
        http.post(
            "/attendance/punch-out",
            json={"emp_code": "EMP1001", "punched_at": ms("2026-07-06T20:00:00")},
        ).status_code
        == 409
    )
    patch = {
        "reason": "clock correction",
        "regularized_by": "hr",
        "punch_in": ms("2026-07-06T09:30:00"),
        "punch_out": ms("2026-07-06T18:30:00"),
    }
    r = http.patch("/attendance/EMP1001/2026-07-06", json=patch)
    assert r.status_code == 200
    check(r.json(), "AttendanceRecord")
    stored = db.attendance_logs.find_one({"emp_code": "EMP1001"})
    for field in ("punch_in", "punch_out"):
        for direction in ("from", "to"):
            assert isinstance(stored["history"][0]["changes"][field][direction], datetime)
            assert isinstance(r.json()["history"][0]["changes"][field][direction], int)
    assert http.patch("/attendance/EMP1001/2026-07-06", json=patch).status_code == 422
    base = {"reason": "valid reason", "regularized_by": "hr"}
    for edit in (
        {"status": "LEAVE", "punch_in": ms("2026-07-06T09:30:00")},
        {"punch_in": ms("2026-07-07T09:30:00")},
        {"punch_out": None},
    ):
        assert http.patch("/attendance/EMP1001/2026-07-06", json=base | edit).status_code == 422
    r = http.patch("/attendance/EMP1001/2026-07-06", json=base | {"status": "LEAVE"})
    assert r.status_code == 200 and len(r.json()["history"]) == 2
    assert all(r.json()[k] is None for k in ("punch_in", "punch_out", "work_hours"))
    assert (
        http.patch("/attendance/EMP1001/2026-07-06", json=base | {"status": "PRESENT"}).status_code
        == 422
    )
    assert http.patch("/attendance/EMP1001/2026-07-07", json=base).status_code == 404
    assert http.patch("/attendance/missing/2026-07-07", json=base).status_code == 404
    check(http.get("/attendance").json(), "AttendancePage")
    for params in (
        {"date_from": "2026-07-07", "date_to": "2026-07-06"},
        {"date_from": "2026-02-30"},
        {"status": "BAD"},
    ):
        assert http.get("/attendance", params=params).status_code == 422
    assert (
        http.get("/analytics/employees/missing/monthly", params={"month": "2026-07"}).status_code
        == 404
    )
    assert (
        http.get(
            "/analytics/departments/missing/trend",
            params={"from": "2026-07-01", "to": "2026-07-01"},
        ).status_code
        == 404
    )
    for endpoint in (
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
        "invalid",
    ):
        assert http.get("/admin/explain/" + endpoint).status_code == 422


def test_default_clock_night_and_exact_duration(api):
    http, db = api
    assert (
        http.post("/employees", json=employee(shift_start="22:00", shift_end="06:00")).status_code
        == 201
    )
    r = http.post(
        "/attendance/punch-in",
        json={"emp_code": "EMP1001", "punched_at": ms("2026-01-01T00:00:00")},
    )
    assert r.status_code == 201 and r.json()["date"] == "2025-12-31"
    r = http.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP1001", "punched_at": ms("2026-01-02T00:00:00")},
    )
    assert r.status_code == 200 and r.json()["work_hours"] == 24
    assert http.post("/employees", json=employee("EMP1002")).status_code == 201
    before = int(datetime.now(timezone.utc).timestamp()) * 1000
    r = http.post("/attendance/punch-in", json={"emp_code": "EMP1002"})
    assert (
        r.status_code == 201
        and before <= r.json()["punch_in"] <= int(datetime.now(timezone.utc).timestamp()) * 1000
    )


def test_business_validation_error_shape(api):
    r = api[0].get("/attendance", params={"date_from": "2026-07-02", "date_to": "2026-07-01"})
    assert r.status_code == 422 and isinstance(r.json()["detail"], list)


@pytest.mark.parametrize("value", [100000000000, 4102444800000])
def test_epoch_boundaries_http(api, value):
    http, _ = api
    assert http.post("/employees", json=employee()).status_code == 201
    r = http.post("/attendance/punch-in", json={"emp_code": "EMP1001", "punched_at": value})
    assert r.status_code == 201 and r.json()["punch_in"] == value


def test_reject_nonpresence_and_ignore_derived(api):
    http, _ = api
    assert http.post("/employees", json=employee()).status_code == 201
    for status in ("ABSENT", "LEAVE", "present", None):
        assert (
            http.post(
                "/attendance/punch-in", json={"emp_code": "EMP1001", "status": status}
            ).status_code
            == 422
        )
    r = http.post(
        "/attendance/punch-in",
        json={
            "emp_code": "EMP1001",
            "punched_at": ms("2026-07-06T09:30:00"),
            "half_day": True,
            "id": "fake",
        },
    )
    assert r.status_code == 201 and r.json()["half_day"] is False and "id" not in r.json()
    body = {"reason": "valid reason", "regularized_by": "hr", "work_hours": 999}
    assert http.patch("/attendance/EMP1001/2026-07-06", json=body).status_code == 422
