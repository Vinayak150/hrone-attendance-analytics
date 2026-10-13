"""Independent Python reference calculations exist only in tests, never production."""

import calendar
import random
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from tests.test_contract import check, employee

PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}


def rounded(value, places=2):
    return float(Decimal(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def units(row):
    return Decimal("0.5") if row.get("half_day", False) else Decimal(1)


def days_between(first, last):
    return [first + timedelta(days=n) for n in range((last - first).days + 1)]


def dataset():
    rng = random.Random(275)
    employees = [
        employee(
            f"EMP{i:04}",
            department=["Engineering", "Sales", "NoLogs", "Future"][min(i // 8, 3)],
            joined_on=("2026-08-01" if i >= 24 else "2026-07-13" if i % 4 == 0 else "2026-01-01"),
            shift_start="09:30",
            shift_end="18:30",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        for i in range(1, 26)
    ]
    logs = []
    for emp in employees:
        if emp["department"] == "NoLogs":
            continue
        for day in days_between(date(2026, 7, 1), date(2026, 7, 31)):
            if rng.random() < 0.30:
                continue
            status = rng.choice(["PRESENT", "WFH", "ON_DUTY", "LEAVE", "ABSENT"])
            row = dict(
                emp_code=emp["emp_code"],
                date=str(day),
                status=status,
                punch_in=None,
                punch_out=None,
                work_hours=rng.choice([None, 4.12, 4.13, 7.01, 8.0]),
                late_minutes=rng.choice([0, 0, 10, 15, 35]),
                overtime_minutes=rng.choice([0, 0, 30, 40]),
                half_day=rng.choice([True, False]),
                history=[],
            )
            if rng.random() < 0.2:
                for key in ("history", "half_day", "late_minutes", "overtime_minutes"):
                    row.pop(key)
            logs.append(row)
    logs.append(dict(emp_code="orphan", date="2026-07-01", status="PRESENT", late_minutes=99999))
    return employees, logs


def monthly_reference(emp, logs, month):
    y, m = map(int, month.split("-"))
    first = date(y, m, 1)
    last = date(y, m, calendar.monthrange(y, m)[1])
    records = [r for r in logs if r["emp_code"] == emp["emp_code"] and r["date"].startswith(month)]
    working = sum(
        d.weekday() < 5
        for d in days_between(max(first, date.fromisoformat(emp["joined_on"])), last)
    )
    present = sum(
        (
            units(r)
            for r in records
            if r["status"] in PRESENCE and date.fromisoformat(r["date"]).weekday() < 5
        ),
        Decimal(0),
    )
    return dict(
        emp_code=emp["emp_code"],
        month=month,
        working_days=working,
        present_days=float(present),
        leave_days=sum(r["status"] == "LEAVE" for r in records),
        late_count=sum(r.get("late_minutes", 0) > 0 for r in records),
        total_late_minutes=sum(r.get("late_minutes", 0) for r in records),
        total_overtime_minutes=sum(r.get("overtime_minutes", 0) for r in records),
        attendance_pct=rounded(present / working * 100) if working else None,
    )


def summary_reference(employees, logs, month, department=None):
    last = month + "-" + str(calendar.monthrange(*map(int, month.split("-")))[1])
    qualifying = [
        e
        for e in employees
        if e["joined_on"] <= last and (department is None or e["department"] == department)
    ]
    items = []
    for dep in sorted({e["department"] for e in qualifying}):
        staff = [e for e in qualifying if e["department"] == dep]
        codes = {e["emp_code"] for e in staff}
        records = [r for r in logs if r["emp_code"] in codes and r["date"].startswith(month)]
        hours = [
            Decimal(str(r["work_hours"]))
            for r in records
            if r["status"] in PRESENCE and r.get("work_hours") is not None
        ]
        items.append(
            dict(
                department=dep,
                headcount=len(staff),
                present_days=float(
                    sum(
                        (
                            units(r)
                            for r in records
                            if r["status"] in PRESENCE
                            and date.fromisoformat(r["date"]).weekday() < 5
                        ),
                        Decimal(0),
                    )
                ),
                avg_work_hours=rounded(sum(hours) / len(hours)) if hours else None,
                late_count=sum(r.get("late_minutes", 0) > 0 for r in records),
                total_late_minutes=sum(r.get("late_minutes", 0) for r in records),
                leave_count=sum(r["status"] == "LEAVE" for r in records),
                on_duty_count=sum(r["status"] == "ON_DUTY" for r in records),
            )
        )
    return dict(month=month, items=items)


def leaderboard_reference(employees, logs, month, limit, department=None):
    items = []
    for emp in employees:
        if department is not None and emp["department"] != department:
            continue
        records = [
            r for r in logs if r["emp_code"] == emp["emp_code"] and r["date"].startswith(month)
        ]
        late = sum(r.get("late_minutes", 0) for r in records)
        if late > 0:
            items.append(
                dict(
                    emp_code=emp["emp_code"],
                    name=emp["name"],
                    department=emp["department"],
                    total_late_minutes=late,
                    late_count=sum(r.get("late_minutes", 0) > 0 for r in records),
                )
            )
    items.sort(key=lambda r: (-r["total_late_minutes"], r["emp_code"]))
    for i, row in enumerate(items):
        row["rank"] = (
            i + 1
            if i == 0 or items[i - 1]["total_late_minutes"] != row["total_late_minutes"]
            else items[i - 1]["rank"]
        )
    return dict(month=month, items=[r for r in items if r["rank"] <= limit])


def trend_reference(employees, logs, dep, first, last):
    staff = [e for e in employees if e["department"] == dep]
    codes = {e["emp_code"] for e in staff}
    items = []
    for day in days_between(date.fromisoformat(first), date.fromisoformat(last)):
        records = [r for r in logs if r["emp_code"] in codes and r["date"] == str(day)]
        headcount = sum(e["joined_on"] <= str(day) for e in staff)
        present = sum((units(r) for r in records if r["status"] in PRESENCE), Decimal(0))
        rate = rounded(present / headcount, 4) if day.weekday() < 5 and headcount else None
        row = dict(
            date=str(day),
            is_working_day=day.weekday() < 5,
            headcount=headcount,
            present_count=float(present),
            late_count=sum(r.get("late_minutes", 0) > 0 for r in records),
            attendance_rate=rate,
        )
        items.append(row)
        rates = [
            Decimal(str(r["attendance_rate"]))
            for r in items[-7:]
            if r["attendance_rate"] is not None
        ]
        row["moving_avg_7d"] = rounded(sum(rates) / len(rates), 4) if rates else None
    return dict(department=dep, items=items)


def test_all_analytics_against_reference(api):
    http, db = api
    employees, logs = dataset()
    db.employees.insert_many([dict(e) for e in employees])
    db.attendance_logs.insert_many([dict(r) for r in logs])
    for month in ("2026-07", "2026-02", "2024-02", "2026-08"):
        for emp in employees:
            r = http.get(f"/analytics/employees/{emp['emp_code']}/monthly", params={"month": month})
            assert r.status_code == 200, r.text
            check(r.json(), "EmployeeMonthly")
            assert r.json() == monthly_reference(emp, logs, month)
        for dep in (None, "Engineering", "NoLogs", "Future", "engineering"):
            params = {"month": month} | ({"department": dep} if dep is not None else {})
            r = http.get("/analytics/departments/summary", params=params)
            assert r.status_code == 200, r.text
            check(r.json(), "DepartmentSummary")
            assert r.json() == summary_reference(employees, logs, month, dep)
            for limit in (1, 2, 10, 50):
                r = http.get("/analytics/leaderboard/late", params=params | {"limit": limit})
                assert r.status_code == 200, r.text
                check(r.json(), "Leaderboard")
                assert r.json() == leaderboard_reference(employees, logs, month, limit, dep)
    for dep in ("Engineering", "Sales", "NoLogs", "Future"):
        for first, last in (
            ("2026-07-01", "2026-07-31"),
            ("2026-07-04", "2026-07-05"),
            ("2026-07-08", "2026-07-17"),
            ("2026-06-01", "2026-08-31"),
        ):
            r = http.get(
                "/analytics/departments/" + dep + "/trend", params={"from": first, "to": last}
            )
            assert r.status_code == 200, r.text
            check(r.json(), "Trend")
            assert r.json() == trend_reference(employees, logs, dep, first, last)
    for first, last in (("2026-06-01", "2026-09-01"), ("2026-07-02", "2026-07-01")):
        assert (
            http.get(
                "/analytics/departments/Engineering/trend", params={"from": first, "to": last}
            ).status_code
            == 422
        )


def test_competition_rank_cutoff_and_decimal_ties(api):
    http, db = api
    staff = [employee(f"EMP{i:04}", shift_start="09:30", shift_end="18:30") for i in range(1, 7)]
    db.employees.insert_many(staff)
    for i, total in enumerate([90, 60, 60, 60, 30, 0], 1):
        db.attendance_logs.insert_one(
            dict(
                emp_code=f"EMP{i:04}",
                date="2026-07-06",
                status="PRESENT",
                late_minutes=total,
                work_hours=4.125,
            )
        )
    r = http.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 2}).json()
    assert [v["rank"] for v in r["items"]] == [1, 2, 2, 2]
    r = http.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 5}).json()
    assert [v["rank"] for v in r["items"]] == [1, 2, 2, 2, 5]
    assert (
        http.get("/analytics/departments/summary", params={"month": "2026-07"}).json()["items"][0][
            "avg_work_hours"
        ]
        == 4.13
    )
    # Rate tie 1/32 = .03125 -> .0313; moving average tie (.0313+0)/2 -> .0157.
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    db.employees.insert_many([employee(f"EMP{i:04}") for i in range(1, 33)])
    db.attendance_logs.insert_one(dict(emp_code="EMP0001", date="2026-07-06", status="PRESENT"))
    items = http.get(
        "/analytics/departments/Engineering/trend",
        params={"from": "2026-07-06", "to": "2026-07-07"},
    ).json()["items"]
    assert items[0]["attendance_rate"] == 0.0313 and items[1]["moving_avg_7d"] == 0.0157
