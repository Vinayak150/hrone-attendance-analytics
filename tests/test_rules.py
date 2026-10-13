"""Deterministic contract checks; these do not require a running MongoDB."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.main import (
    EmployeeIn,
    PunchInIn,
    RegularizeIn,
    attendance_filter,
    attendance_json,
    checked_trend_range,
    computed_values,
    datetime_to_milliseconds,
    leaderboard_pipeline,
    local_shift_date,
    millisecond_to_datetime,
    month_bounds,
    monthly_pipeline,
    trend_pipeline,
)


def ist(text):
    return (
        datetime.fromisoformat(text)
        .replace(tzinfo=ZoneInfo("Asia/Kolkata"))
        .astimezone(timezone.utc)
    )


NORMAL = {"shift_start": "09:30", "shift_end": "18:30"}
NIGHT = {"shift_start": "22:00", "shift_end": "06:00"}


@pytest.mark.parametrize(
    "clock,expected",
    [
        ("09:29:00", 0),
        ("09:40:00", 0),
        ("09:40:01", 10),
        ("09:40:59", 10),
        ("10:15:59", 45),
    ],
)
def test_grace_is_strict_and_late_minutes_are_floored(clock, expected):
    assert (
        computed_values(ist("2026-07-06T" + clock), None, "PRESENT", "2026-07-06", NORMAL)[
            "late_minutes"
        ]
        == expected
    )


@pytest.mark.parametrize(
    "milliseconds", [1000000000, 1.5e12, "1783312500000", None, True, 4102444800001]
)
def test_reject_invalid_explicit_epoch_milliseconds(milliseconds):
    with pytest.raises(ValidationError):
        PunchInIn(emp_code="EMP0001", punched_at=milliseconds)


def test_truncates_fractional_second():
    assert millisecond_to_datetime(1783312500999).microsecond == 0


def test_overlapping_overnight_calendar_date():
    assert local_shift_date(ist("2026-07-07T05:59:59"), **NIGHT) == "2026-07-06"
    assert local_shift_date(ist("2026-07-07T06:00:00"), **NIGHT) == "2026-07-07"
    result = computed_values(
        ist("2026-07-06T21:55:00"), ist("2026-07-07T06:40:00"), "PRESENT", "2026-07-06", NIGHT
    )
    assert result == {
        "late_minutes": 0,
        "work_hours": 8.75,
        "overtime_minutes": 40,
        "half_day": False,
    }


def test_overtime_minimum_boundary():
    inside = ist("2026-07-06T09:00:00")
    assert (
        computed_values(inside, ist("2026-07-06T18:59:59"), "PRESENT", "2026-07-06", NORMAL)[
            "overtime_minutes"
        ]
        == 0
    )
    assert (
        computed_values(inside, ist("2026-07-06T19:00:00"), "PRESENT", "2026-07-06", NORMAL)[
            "overtime_minutes"
        ]
        == 30
    )


def test_work_hours_half_up_before_half_day_decision():
    inside = ist("2026-07-06T09:00:00")
    low = computed_values(inside, ist("2026-07-06T13:29:41"), "PRESENT", "2026-07-06", NORMAL)
    tie = computed_values(inside, ist("2026-07-06T13:29:42"), "PRESENT", "2026-07-06", NORMAL)
    assert (low["work_hours"], low["half_day"]) == (4.49, True)
    assert (tie["work_hours"], tie["half_day"]) == (4.50, False)
    # 4h 7m 30s = exactly 4.125h, a half-even/half-up discriminator.
    v = computed_values(inside, ist("2026-07-06T13:07:30"), "PRESENT", "2026-07-06", NORMAL)
    assert v["work_hours"] == 4.13


def test_employee_validation():
    values = {
        "emp_code": "EMP0001",
        "name": "Test",
        "email": "x@example.org",
        "department": "Engineering",
        "joined_on": "2026-02-30",
    }
    with pytest.raises(ValidationError):
        EmployeeIn(**values)
    with pytest.raises(ValidationError):
        EmployeeIn(
            **(values | {"joined_on": "2026-07-06", "shift_start": "09:30", "shift_end": "09:30"})
        )
    with pytest.raises(ValidationError):
        RegularizeIn(reason="bad", regularized_by="someone", status="PRESENT")


def test_invalid_ranges():
    assert month_bounds("2026-02") == ("2026-02-01", "2026-03-01", "2026-02-28")
    assert month_bounds("2024-02") == ("2024-02-01", "2024-03-01", "2024-02-29")
    with pytest.raises(HTTPException) as err:
        checked_trend_range("2026-01-01", "2026-04-04")
    assert err.value.status_code == 422
    with pytest.raises(HTTPException) as err:
        attendance_filter(None, "2026-07-31", "2026-07-01", None)
    assert err.value.status_code == 422


def test_legacy_record_and_nested_audit_serializer():
    old = {
        "emp_code": "EMP0001",
        "date": "2026-07-06",
        "status": "PRESENT",
        "punch_in": ist("2026-07-06T09:30:00"),
        "punch_out": None,
    }
    converted = attendance_json(old)
    assert (converted["history"], converted["half_day"], converted["work_hours"]) == (
        [],
        False,
        None,
    )
    old["history"] = [
        {
            "at": ist("2026-07-07T10:00:00"),
            "by": "HR",
            "reason": "fix punch",
            "changes": {
                "punch_in": {"from": ist("2026-07-06T09:45:00"), "to": ist("2026-07-06T09:30:00")}
            },
        }
    ]
    assert converted["punch_in"] == datetime_to_milliseconds(old["punch_in"])
    assert isinstance(attendance_json(old)["history"][0]["changes"]["punch_in"]["from"], int)


def test_pipeline_structure_for_ranking_and_gap_filling():
    ranking = leaderboard_pipeline("2026-07", 2)
    assert any("$setWindowFields" in s and "$rank" in str(s) for s in ranking)
    assert any("$match" in s and s["$match"] == {"rank": {"$lte": 2}} for s in ranking)
    monthly = monthly_pipeline("EMP0001", "2026-07")
    assert monthly[0] == {"$match": {"emp_code": "EMP0001"}}
    trend = trend_pipeline("Engineering", "2026-07-01", "2026-07-31")
    assert any(
        "$setWindowFields" in s and "average_decimal" in s["$setWindowFields"]["output"]
        for s in trend
    )
    assert any("$project" in s and "dates" in s["$project"] and "$range" in str(s) for s in trend)


@pytest.mark.parametrize("milliseconds", [100000000000, 4102444800000])
def test_epoch_inclusive_limits(milliseconds):
    assert PunchInIn(emp_code="EMP0001", punched_at=milliseconds).punched_at == milliseconds


@pytest.mark.parametrize(
    "day,expected",
    [
        ("2026-01-01T00:00:00", "2025-12-31"),
        ("2026-03-01T05:59:59", "2026-02-28"),
        ("2024-03-01T05:59:59", "2024-02-29"),
    ],
)
def test_overnight_month_year_leap(day, expected):
    assert local_shift_date(ist(day), **NIGHT) == expected


def test_trend_inclusive_limit():
    assert checked_trend_range("2026-06-01", "2026-08-31") == 92
    with pytest.raises(HTTPException):
        checked_trend_range("2026-06-01", "2026-09-01")


@pytest.mark.parametrize("clock,expected", [("18:59:59", 0), ("19:00:00", 30), ("19:00:59", 30)])
def test_overtime_whole_minutes(clock, expected):
    assert (
        computed_values(
            ist("2026-07-06T09:30:00"), ist("2026-07-06T" + clock), "PRESENT", "2026-07-06", NORMAL
        )["overtime_minutes"]
        == expected
    )
