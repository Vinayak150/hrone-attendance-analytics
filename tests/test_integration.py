"""Explain modes use production builders, including all list parameters."""

import pytest

import app.main as service
from tests.test_analytics import dataset


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


@pytest.mark.parametrize(
    "endpoint,params",
    [
        ("attendance_list", {}),
        (
            "attendance_list",
            {
                "emp_code": "EMP0001",
                "date_from": "2026-07-01",
                "date_to": "2026-07-31",
                "status": "WFH",
                "page": 2,
                "page_size": 3,
            },
        ),
        ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
        ("department_summary", {"month": "2026-07"}),
        ("department_summary", {"month": "2026-07", "department": "Engineering"}),
        ("late_leaderboard", {"month": "2026-07", "department": "Engineering", "limit": 2}),
        (
            "department_trend",
            {"department": "Engineering", "from": "2026-07-01", "to": "2026-07-31"},
        ),
    ],
)
def test_explain_shared_query(api, endpoint, params):
    http, db = api
    staff, logs = dataset()
    db.employees.insert_many(staff)
    db.attendance_logs.insert_many(logs)
    r = http.get("/admin/explain/" + endpoint, params=params)
    assert r.status_code == 200, r.text
    plan = r.json()["explain"]
    nodes = list(walk(plan))
    stages = {n.get("stage") for n in nodes}
    assert "IXSCAN" in stages and "COLLSCAN" not in stages
    command = plan["command"]
    if endpoint == "attendance_list":
        expected = service.attendance_command(
            params.get("emp_code"),
            params.get("date_from"),
            params.get("date_to"),
            params.get("status"),
            params.get("page", 1),
            params.get("page_size", 20),
        )
        assert all(command[k] == v for k, v in expected.items())
    else:
        builders = {
            "employee_monthly": lambda: service.monthly_pipeline(
                params["emp_code"], params["month"]
            ),
            "department_summary": lambda: service.department_pipeline(
                params["month"], params.get("department")
            ),
            "late_leaderboard": lambda: service.leaderboard_pipeline(
                params["month"], params.get("limit", 10), params.get("department")
            ),
            "department_trend": lambda: service.trend_pipeline(
                params["department"], params["from"], params["to"]
            ),
        }
        assert (
            command["pipeline"]
            == service.bson_to_json({"pipeline": builders[endpoint]()})["pipeline"]
        )
