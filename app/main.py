"""HROne attendance service: API boundary, atomic writes, and MongoDB-side analytics."""

from __future__ import annotations

import calendar
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from threading import Lock
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from bson import Decimal128, json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from starlette.concurrency import run_in_threadpool

load_dotenv(override=False)
UTC = timezone.utc
IST = ZoneInfo("Asia/Kolkata")
PRESENT = ("PRESENT", "WFH", "ON_DUTY")
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresentStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
EpochMillis = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
DateString = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]
MonthQuery = Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]
Limit = Annotated[int, Query(ge=1, le=50)]

client = MongoClient(
    os.environ.get("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=UTC,
    serverSelectionTimeoutMS=2500,
)
db = client[os.environ.get("MONGO_DB", "attendance_db")]


_index_lock = Lock()
_indexes_ready = False


def ensure_indexes():
    """Retry startup initialization before readiness/writes after an initial outage."""
    global _indexes_ready
    with _index_lock:
        if _indexes_ready:
            return
        db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="employee_code_uq")
        db.employees.create_index(
            [("department", ASCENDING), ("emp_code", ASCENDING)], name="department_code"
        )
        db.employees.create_index(
            [("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined"
        )
        db.employees.create_index([("joined_on", ASCENDING)], name="joined_on")
        db.attendance_logs.create_index(
            [("emp_code", ASCENDING), ("date", ASCENDING)],
            unique=True,
            name="attendance_natural_key_uq",
        )
        db.attendance_logs.create_index(
            [("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_list_order"
        )
        db.attendance_logs.create_index(
            [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
            name="attendance_status_order",
        )
        db.attendance_logs.create_index(
            [("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="employee_latest_punch"
        )
        db.attendance_logs.create_index(
            [("date", ASCENDING), ("late_minutes", ASCENDING)], name="attendance_date_late"
        )
        _indexes_ready = True


@asynccontextmanager
async def lifespan(application: FastAPI):
    global _indexes_ready
    _indexes_ready = False
    try:
        await run_in_threadpool(ensure_indexes)
    except PyMongoError:
        # Keep readiness reachable, but never accept writes without unique indexes.
        pass
    try:
        yield
    finally:
        client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def database_unavailable(request, exc):
    return JSONResponse(status_code=503, content={"detail": "MongoDB is unavailable"})


# ------ Time, validation, and serializers ----------------------------------
def invalid(message: str) -> HTTPException:
    return HTTPException(422, [{"type": "value_error", "loc": ["request"], "msg": message}])


def parse_day(value: str) -> date:
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("bad date shape")
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        raise ValueError("date must be a real YYYY-MM-DD calendar date") from None


def month_bounds(month: str) -> tuple[str, str, str]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise invalid("month must be YYYY-MM")
    try:
        first = parse_day(f"{month}-01")
        last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
        next_month = (
            f"{first.year + 1:04}-01-01"
            if first.month == 12
            else f"{first.year:04}-{first.month + 1:02}-01"
        )
    except ValueError:
        raise invalid("unsupported month") from None
    return first.isoformat(), next_month, last.isoformat()


def millisecond_to_datetime(ms: int) -> datetime:
    return datetime.fromtimestamp(ms // 1000, UTC)  # truncation R1


def datetime_to_milliseconds(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:  # existing legacy data returned by default PyMongo
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp()) * 1000


def utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def local_shift_date(punch: datetime, shift_start: str, shift_end: str) -> str:
    local = punch.astimezone(IST)
    day = local.date()
    if shift_end <= shift_start and local.strftime("%H:%M") < shift_end:
        day -= timedelta(days=1)
    return day.isoformat()


def shift_times(day: str, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    day_obj = parse_day(day)
    hh, mm = map(int, shift_start.split(":"))
    start = datetime.combine(day_obj, time(hh, mm), IST)
    hh, mm = map(int, shift_end.split(":"))
    finish = datetime.combine(
        day_obj + timedelta(days=(shift_end <= shift_start)), time(hh, mm), IST
    )
    return start, finish


def computed_values(
    punch_in: datetime | None, punch_out: datetime | None, status: str, day: str, employee: dict
) -> dict:
    if status not in PRESENT:
        return {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    start, end = shift_times(day, employee["shift_start"], employee["shift_end"])
    elapsed = int((punch_in - start).total_seconds())
    late = elapsed // 60 if elapsed > 600 else 0
    result = {"late_minutes": late, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    if punch_out is not None:
        seconds = int((punch_out - punch_in).total_seconds())
        hours = (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        overtime = int((punch_out - end).total_seconds()) // 60
        result.update(
            work_hours=float(hours),
            overtime_minutes=overtime if overtime >= 30 else 0,
            half_day=hours < Decimal("4.50"),
        )
    return result


def validate_duration(punch_in: datetime, punch_out: datetime | None) -> None:
    if punch_out is not None:
        seconds = (punch_out - punch_in).total_seconds()
        if seconds <= 0 or seconds > 86400:
            raise invalid("punch_out must be later than punch_in and within 24 hours")


def employee_json(doc: dict) -> dict:
    return {
        k: (datetime_to_milliseconds(v) if k == "created_at" else v)
        for k, v in doc.items()
        if k != "_id"
    }


def attendance_json(doc: dict) -> dict:
    data = {k: v for k, v in doc.items() if k != "_id"}
    for name in ("punch_in", "punch_out"):
        data[name] = datetime_to_milliseconds(data.get(name))
    data.setdefault("work_hours", None)
    data.setdefault("late_minutes", 0)
    data.setdefault("overtime_minutes", 0)
    data.setdefault("half_day", False)
    data["history"] = []
    for entry in doc.get("history") or []:
        changes = {}
        for key, pair in entry.get("changes", {}).items():
            changes[key] = {
                direction: (
                    datetime_to_milliseconds(val) if key in ("punch_in", "punch_out") else val
                )
                for direction, val in pair.items()
            }
        data["history"].append(
            {
                "at": datetime_to_milliseconds(entry["at"]),
                "by": entry["by"],
                "reason": entry["reason"],
                "changes": changes,
            }
        )
    return data


def bson_to_json(value: dict) -> dict:
    import json

    return json.loads(json_util.dumps(value))


# ------ Validated requests -------------------------------------------------
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: DateString

    @field_validator("joined_on")
    @classmethod
    def real_day(cls, value: str) -> str:
        parse_day(value)
        return value

    @model_validator(mode="after")
    def distinct_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: EpochMillis | None = None
    status: PresentStatus = "PRESENT"

    @model_validator(mode="before")
    @classmethod
    def no_null_timestamp(cls, obj):
        if isinstance(obj, dict) and "punched_at" in obj and obj["punched_at"] is None:
            raise ValueError("punched_at cannot be null")
        return obj


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: EpochMillis | None = None

    @model_validator(mode="before")
    @classmethod
    def no_null_timestamp(cls, obj):
        if isinstance(obj, dict) and "punched_at" in obj and obj["punched_at"] is None:
            raise ValueError("punched_at cannot be null")
        return obj


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    status: Status | None = None
    punch_in: EpochMillis | None = None
    punch_out: EpochMillis | None = None

    @model_validator(mode="before")
    @classmethod
    def no_explicit_nulls(cls, obj):
        if isinstance(obj, dict) and any(
            k in obj and obj[k] is None for k in ("status", "punch_in", "punch_out")
        ):
            raise ValueError("status and punch times cannot be explicitly null")
        return obj


# ------ CRUD ---------------------------------------------------------------
@app.get("/health")
def health():
    try:
        db.command("ping")
        ensure_indexes()
    except PyMongoError:
        raise HTTPException(503, "MongoDB is unavailable") from None
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    ensure_indexes()
    doc = body.model_dump()
    doc["created_at"] = utc_now()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists") from None
    return employee_json(doc)


@app.get("/employees")
def list_employees(department: str | None = None, page: Page = 1, page_size: PageSize = 20):
    query = {"department": department} if department is not None else {}
    docs = (
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [employee_json(x) for x in docs],
        "total": db.employees.count_documents(query),
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    ensure_indexes()
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    punched_at = (
        millisecond_to_datetime(body.punched_at) if body.punched_at is not None else utc_now()
    )
    day = local_shift_date(punched_at, employee["shift_start"], employee["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "history": [],
        **computed_values(punched_at, None, body.status, day, employee),
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "attendance record already exists for this date") from None
    return attendance_json(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    ensure_indexes()
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    punched_at = (
        millisecond_to_datetime(body.punched_at) if body.punched_at is not None else utc_now()
    )
    latest = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}},
        sort=[("punch_in", DESCENDING)],
    )
    if latest is None:
        raise HTTPException(404, "no punch-in found")
    if latest.get("punch_out") is not None:
        raise HTTPException(409, "record already punched out")
    validate_duration(latest["punch_in"], punched_at)
    values = computed_values(
        latest["punch_in"], punched_at, latest["status"], latest["date"], employee
    )
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": latest["_id"],
            "punch_in": latest["punch_in"],
            "punch_out": None,
            "status": latest["status"],
            "history": latest["history"] if "history" in latest else {"$exists": False},
        },
        {"$set": {"punch_out": punched_at, **values}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "attendance was modified concurrently")
    return attendance_json(updated)


def attendance_filter(
    emp_code: str | None, date_from: str | None, date_to: str | None, status: Status | None
) -> dict:
    if date_from is not None:
        try:
            parse_day(date_from)
        except ValueError:
            raise invalid("invalid date_from") from None
    if date_to is not None:
        try:
            parse_day(date_to)
        except ValueError:
            raise invalid("invalid date_to") from None
    if date_from is not None and date_to is not None and date_from > date_to:
        raise invalid("date_from cannot be after date_to")
    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if status is not None:
        query["status"] = status
    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from
        if date_to is not None:
            query["date"]["$lte"] = date_to
    return query


def attendance_command(emp_code, date_from, date_to, status, page, page_size):
    return {
        "find": "attendance_logs",
        "filter": attendance_filter(emp_code, date_from, date_to, status),
        "projection": {"_id": 0},
        "sort": {"date": -1, "emp_code": 1},
        "skip": (page - 1) * page_size,
        "limit": page_size,
    }


@app.get("/attendance")
def list_attendance(
    emp_code: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    status: Status | None = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    command = attendance_command(emp_code, date_from, date_to, status, page, page_size)
    rows = db.attendance_logs.find(command["filter"], command["projection"]).sort(
        list(command["sort"].items())
    )
    rows = rows.skip(command["skip"]).limit(command["limit"])
    return {
        "items": [attendance_json(x) for x in rows],
        "total": db.attendance_logs.count_documents(command["filter"]),
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    ensure_indexes()
    try:
        parse_day(date)
    except ValueError:
        raise invalid("invalid attendance date") from None
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if old is None:
        raise HTTPException(404, "attendance record not found")
    fields = body.model_fields_set
    status = body.status if "status" in fields else old["status"]
    if status in ("ABSENT", "LEAVE"):
        if "punch_in" in fields or "punch_out" in fields:
            raise invalid("absence and leave cannot contain punch times")
        punch_in = punch_out = None
    else:
        punch_in = (
            millisecond_to_datetime(body.punch_in) if "punch_in" in fields else old.get("punch_in")
        )
        punch_out = (
            millisecond_to_datetime(body.punch_out)
            if "punch_out" in fields
            else old.get("punch_out")
        )
        if punch_in is None:
            raise invalid("presence status requires punch_in")
        if local_shift_date(punch_in, employee["shift_start"], employee["shift_end"]) != date:
            raise invalid("punch_in belongs to a different attendance date")
        validate_duration(punch_in, punch_out)

    after = {
        "status": status,
        "punch_in": punch_in,
        "punch_out": punch_out,
        **computed_values(punch_in, punch_out, status, date, employee),
    }
    previous = {
        "status": old["status"],
        "punch_in": old.get("punch_in"),
        "punch_out": old.get("punch_out"),
        "late_minutes": old.get("late_minutes", 0),
        "work_hours": old.get("work_hours"),
        "overtime_minutes": old.get("overtime_minutes", 0),
        "half_day": old.get("half_day", False),
    }
    changes = {
        key: {"from": previous[key], "to": new_value}
        for key, new_value in after.items()
        if previous[key] != new_value
    }
    if not changes:
        raise invalid("regularization changes nothing")
    history = {
        "at": utc_now(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    # CAS protects both the original attendance values and the complete audit chain.
    history_match = (
        {"history": old["history"]} if "history" in old else {"history": {"$exists": False}}
    )
    condition = {
        "_id": old["_id"],
        "status": old["status"],
        "punch_in": old.get("punch_in"),
        "punch_out": old.get("punch_out"),
        **history_match,
    }
    updated = db.attendance_logs.find_one_and_update(
        condition,
        {"$set": after, "$push": {"history": history}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "attendance was modified concurrently")
    return attendance_json(updated)


# ------ Mongo aggregation expressions / shared pipeline builders -----------
def decimal_round_half_up(value: object, places: int) -> dict:
    """Round a nonnegative expression using BSON decimal arithmetic (Mongo $round is half-even)."""
    factor = Decimal128(str(10**places))
    return {
        "$divide": [
            {
                "$floor": {
                    "$add": [{"$multiply": [{"$toDecimal": value}, factor]}, Decimal128("0.5")]
                }
            },
            factor,
        ]
    }


def weekdays_expr(joined_on: object, first: str) -> dict:
    start = {"$dateFromString": {"dateString": {"$max": [joined_on, first]}}}
    end = {
        "$dateAdd": {
            "startDate": {"$dateFromString": {"dateString": first}},
            "unit": "month",
            "amount": 1,
        }
    }
    diff = {"$max": [0, {"$dateDiff": {"startDate": start, "endDate": end, "unit": "day"}}]}
    return {
        "$size": {
            "$filter": {
                "input": {"$range": [0, diff]},
                "as": "n",
                "cond": {
                    "$lte": [
                        {
                            "$isoDayOfWeek": {
                                "$dateAdd": {"startDate": start, "unit": "day", "amount": "$$n"}
                            }
                        },
                        5,
                    ]
                },
            }
        }
    }


def weekday_record_expr() -> dict:
    return {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 5]}


def present_units_expr(weekdays_only: bool) -> dict:
    checks = [{"$in": ["$status", list(PRESENT)]}]
    if weekdays_only:
        checks.append(weekday_record_expr())
    return {"$cond": [{"$and": checks}, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}


def late_count_expr() -> dict:
    return {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}


def monthly_pipeline(emp_code: str, month: str) -> list[dict]:
    first, _, last = month_bounds(month)
    stats = [
        {
            "$match": {
                "date": {"$gte": first, "$lte": last},
                "$expr": {"$eq": ["$emp_code", "$$code"]},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": present_units_expr(True)},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": late_count_expr()},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]
    return [
        {"$match": {"emp_code": emp_code}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": stats,
                "as": "matches",
            }
        },
        {
            "$set": {
                "working_days": weekdays_expr("$joined_on", first),
                "stat": {"$first": "$matches"},
            }
        },
        {"$set": {"present_days": {"$ifNull": ["$stat.present_days", 0]}}},
        {
            "$project": {
                "_id": 0,
                "emp_code": 1,
                "month": {"$literal": month},
                "working_days": 1,
                "present_days": 1,
                "leave_days": {"$ifNull": ["$stat.leave_days", 0]},
                "late_count": {"$ifNull": ["$stat.late_count", 0]},
                "total_late_minutes": {"$ifNull": ["$stat.total_late_minutes", 0]},
                "total_overtime_minutes": {"$ifNull": ["$stat.total_overtime_minutes", 0]},
                "attendance_pct": {
                    "$cond": [
                        {"$gt": ["$working_days", 0]},
                        {
                            "$toDouble": decimal_round_half_up(
                                {
                                    "$multiply": [
                                        {
                                            "$divide": [
                                                {"$toDecimal": "$present_days"},
                                                {"$toDecimal": "$working_days"},
                                            ]
                                        },
                                        Decimal128("100"),
                                    ]
                                },
                                2,
                            )
                        },
                        None,
                    ]
                },
            }
        },
    ]


def department_pipeline(month: str, department: str | None = None) -> list[dict]:
    first, _, last = month_bounds(month)
    match: dict = {"joined_on": {"$lte": last}}
    if department is not None:
        match["department"] = department
    per_employee = [
        {
            "$match": {
                "date": {"$gte": first, "$lte": last},
                "$expr": {"$eq": ["$emp_code", "$$code"]},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": present_units_expr(True)},
                "late_count": {"$sum": late_count_expr()},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                "worked_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENT)]},
                                    {"$ne": [{"$ifNull": ["$work_hours", None]}, None]},
                                ]
                            },
                            {"$toDecimal": "$work_hours"},
                            Decimal128("0"),
                        ]
                    }
                },
                "worked_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENT)]},
                                    {"$ne": [{"$ifNull": ["$work_hours", None]}, None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
            }
        },
    ]
    return [
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": per_employee,
                "as": "s",
            }
        },
        {"$set": {"stat": {"$first": "$s"}}},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                **{
                    key: {"$sum": {"$ifNull": [f"$stat.{key}", 0]}}
                    for key in (
                        "present_days",
                        "late_count",
                        "total_late_minutes",
                        "leave_count",
                        "on_duty_count",
                        "worked_count",
                    )
                },
                "worked_sum": {"$sum": {"$ifNull": ["$stat.worked_sum", Decimal128("0")]}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$worked_count", 0]},
                        {
                            "$toDouble": decimal_round_half_up(
                                {"$divide": ["$worked_sum", {"$toDecimal": "$worked_count"}]}, 2
                            )
                        },
                        None,
                    ]
                },
            }
        },
        {"$sort": {"department": 1}},
    ]


def leaderboard_pipeline(month: str, limit: int, department: str | None = None) -> list[dict]:
    first, _, last = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "e",
            }
        },
        {"$unwind": "$e"},
    ]
    if department is not None:
        pipeline.append({"$match": {"e.department": department}})
    pipeline += [
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$e.name",
                "department": "$e.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return pipeline


def checked_trend_range(date_from: str, date_to: str) -> int:
    try:
        first, last = parse_day(date_from), parse_day(date_to)
    except ValueError:
        raise invalid("from and to must be valid YYYY-MM-DD dates") from None
    days = (last - first).days + 1
    if days < 1 or days > 92:
        raise invalid("range must contain between 1 and 92 calendar days")
    return days


def trend_pipeline(department: str, date_from: str, date_to: str) -> list[dict]:
    days = checked_trend_range(date_from, date_to)
    # Restrict employees before probing the natural-key index for their date range.
    # The inner lookup returns at most 92 small daily summaries per employee.
    daily_logs = [
        {"$match": {"department": department}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "date": {"$gte": date_from, "$lte": date_to},
                            "$expr": {"$eq": ["$emp_code", "$$code"]},
                        }
                    },
                    {
                        "$project": {
                            "_id": 0,
                            "date": 1,
                            "present_count": present_units_expr(False),
                            "late_count": late_count_expr(),
                        }
                    },
                ],
                "as": "daily",
            }
        },
        {"$unwind": "$daily"},
        {
            "$group": {
                "_id": "$daily.date",
                "present_count": {"$sum": "$daily.present_count"},
                "late_count": {"$sum": "$daily.late_count"},
            }
        },
    ]
    day_series = {
        "$map": {
            "input": {"$range": [0, days]},
            "as": "offset",
            "in": {
                "$dateToString": {
                    "format": "%Y-%m-%d",
                    "date": {
                        "$dateAdd": {
                            "startDate": {"$dateFromString": {"dateString": date_from}},
                            "unit": "day",
                            "amount": "$$offset",
                        }
                    },
                }
            },
        }
    }
    match_stat = {
        "$first": {
            "$filter": {"input": "$logs", "as": "entry", "cond": {"$eq": ["$$entry._id", "$date"]}}
        }
    }
    return [
        {"$match": {"department": department}},
        {"$sort": {"emp_code": 1}},
        {"$limit": 1},  # one indexed employee anchors the calendar even when no logs exist
        {"$lookup": {"from": "employees", "pipeline": daily_logs, "as": "logs"}},
        {"$project": {"dates": day_series, "logs": 1}},
        {"$unwind": "$dates"},
        {"$set": {"date": "$dates"}},
        {"$set": {"daily": match_stat}},
        {
            "$lookup": {
                "from": "employees",
                "let": {"day": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "department": department,
                            "$expr": {"$lte": ["$joined_on", "$$day"]},
                        }
                    },
                    {"$count": "n"},
                ],
                "as": "headcount_data",
            }
        },
        {
            "$set": {
                "headcount": {"$ifNull": [{"$first": "$headcount_data.n"}, 0]},
                "present_count": {"$ifNull": ["$daily.present_count", 0]},
                "late_count": {"$ifNull": ["$daily.late_count", 0]},
                "is_working_day": {
                    "$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 5]
                },
            }
        },
        {
            "$set": {
                "rate_decimal": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        decimal_round_half_up(
                            {
                                "$divide": [
                                    {"$toDecimal": "$present_count"},
                                    {"$toDecimal": "$headcount"},
                                ]
                            },
                            4,
                        ),
                        None,
                    ]
                }
            }
        },
        {"$sort": {"date": 1}},
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "average_decimal": {"$avg": "$rate_decimal", "window": {"documents": [-6, 0]}}
                },
            }
        },
        {"$sort": {"date": 1}},
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {"$ne": ["$rate_decimal", None]},
                        {"$toDouble": "$rate_decimal"},
                        None,
                    ]
                },
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$average_decimal", None]},
                        {"$toDouble": decimal_round_half_up("$average_decimal", 4)},
                        None,
                    ]
                },
            }
        },
    ]


# ------ Analytics ----------------------------------------------------------
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthQuery):
    values = list(db.employees.aggregate(monthly_pipeline(emp_code, month)))
    if not values:
        raise HTTPException(404, "employee not found")
    return values[0]


@app.get("/analytics/departments/summary")
def department_summary(month: MonthQuery, department: str | None = None):
    return {
        "month": month,
        "items": list(
            db.employees.aggregate(department_pipeline(month, department), allowDiskUse=True)
        ),
    }


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: MonthQuery, limit: Limit = 10, department: str | None = None):
    return {
        "month": month,
        "items": list(
            db.attendance_logs.aggregate(
                leaderboard_pipeline(month, limit, department), allowDiskUse=True
            )
        ),
    }


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str, date_from: str = Query(alias="from"), date_to: str = Query(alias="to")
):
    pipeline = trend_pipeline(department, date_from, date_to)
    rows = list(db.employees.aggregate(pipeline, allowDiskUse=True))
    if not rows:
        raise HTTPException(404, "department not found")
    return {"department": department, "items": rows}


# ------ Explain ------------------------------------------------------------
ExplainEndpoint = Literal[
    "attendance_list",
    "employee_monthly",
    "department_summary",
    "late_leaderboard",
    "department_trend",
]


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainEndpoint,
    emp_code: str | None = None,
    month: str | None = None,
    department: str | None = None,
    limit: Limit = 10,
    date_from: str | None = None,
    date_to: str | None = None,
    status: Status | None = None,
    page: Page = 1,
    page_size: PageSize = 20,
    from_day: str | None = Query(default=None, alias="from"),
    to_day: str | None = Query(default=None, alias="to"),
):
    if endpoint == "attendance_list":
        collection = "attendance_logs"
        command = attendance_command(emp_code, date_from, date_to, status, page, page_size)
    else:
        if (
            endpoint in ("employee_monthly", "department_summary", "late_leaderboard")
            and month is None
        ):
            raise invalid("month query parameter is required")
        if month is not None:
            month_bounds(month)
        if endpoint == "employee_monthly":
            if emp_code is None:
                raise invalid("emp_code query parameter is required")
            collection, pipeline = "employees", monthly_pipeline(emp_code, month)
        elif endpoint == "department_summary":
            collection, pipeline = "employees", department_pipeline(month, department)
        elif endpoint == "late_leaderboard":
            collection, pipeline = "attendance_logs", leaderboard_pipeline(month, limit, department)
        else:
            if department is None or from_day is None or to_day is None:
                raise invalid("department, from and to query parameters are required")
            collection, pipeline = "employees", trend_pipeline(department, from_day, to_day)
        command = {
            "aggregate": collection,
            "pipeline": pipeline,
            "cursor": {},
            "allowDiskUse": True,
        }
    explained = db.command({"explain": command, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": collection, "explain": bson_to_json(explained)}
