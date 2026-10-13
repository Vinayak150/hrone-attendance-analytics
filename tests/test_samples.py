"""Optional original-kit verification; no assignment data dumps are bundled."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from bson import json_util
from fastapi.testclient import TestClient

import app.main as service
from tests.test_analytics import (
    leaderboard_reference,
    monthly_reference,
    summary_reference,
    trend_reference,
)
from tests.test_contract import check


def test_original_sample_seed(mongo, monkeypatch):
    if not os.getenv("CANDIDATE_KIT"):
        pytest.skip("Set CANDIDATE_KIT to the extracted official candidate_kit directory")
    kit = Path(os.environ["CANDIDATE_KIT"]).resolve()
    assert mongo.name.startswith("hrone_test_")
    subprocess.run(
        [sys.executable, str(kit / "sample_seed.py")],
        check=True,
        capture_output=True,
        env=os.environ | {"MONGO_URI": os.environ["TEST_MONGO_URI"], "MONGO_DB": mongo.name},
    )
    assert mongo.employees.count_documents({}) == 6
    assert mongo.attendance_logs.count_documents({}) == 9
    assert set(mongo.employees.index_information()) == {"_id_"}
    employees = json_util.loads((kit / "sample_data/employees.json").read_text())
    logs = json_util.loads((kit / "sample_data/attendance_logs.json").read_text())
    monkeypatch.setattr(service, "db", mongo)
    with TestClient(service.app) as http:
        check(http.get("/employees").json(), "EmployeePage")
        check(http.get("/attendance").json(), "AttendancePage")
        for emp in employees:
            assert http.get(
                f"/analytics/employees/{emp['emp_code']}/monthly", params={"month": "2026-07"}
            ).json() == monthly_reference(emp, logs, "2026-07")
        assert http.get(
            "/analytics/departments/summary", params={"month": "2026-07"}
        ).json() == summary_reference(employees, logs, "2026-07")
        assert http.get(
            "/analytics/leaderboard/late", params={"month": "2026-07"}
        ).json() == leaderboard_reference(employees, logs, "2026-07", 10)
        for dep in ("Engineering", "Sales", "Support"):
            assert http.get(
                "/analytics/departments/" + dep + "/trend",
                params={"from": "2026-07-01", "to": "2026-07-31"},
            ).json() == trend_reference(employees, logs, dep, "2026-07-01", "2026-07-31")
