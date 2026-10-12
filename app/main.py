"""Employee Attendance & Analytics API, contract version 2.0."""
from __future__ import annotations

import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Any, Literal, TypeAlias

from bson import json_util
from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv(override=False)

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000
PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
EpochMillis: TypeAlias = Annotated[StrictInt, Field(ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)]
AttendanceStatus = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

mongo_uri = os.getenv("MONGO_URI")
mongo_db_name = os.getenv("MONGO_DB")
client = MongoClient(mongo_uri, tz_aware=True, serverSelectionTimeoutMS=5000) if mongo_uri else None
db = client[mongo_db_name] if client is not None and mongo_db_name else None
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


class InputModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


def omit_null_schema_default(schema: dict[str, Any]) -> None:
    if schema.get("default", object()) is None:
        schema.pop("default")


class EmployeeCreate(InputModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(json_schema_extra={"format": "date"})

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        parse_date(value)
        return value

    @model_validator(mode="after")
    def validate_shift(self) -> EmployeeCreate:
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(InputModel):
    emp_code: str
    punched_at: EpochMillis = Field(default=None, json_schema_extra=omit_null_schema_default)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, value: int | None) -> int | None:
        if value is not None:
            validate_epoch_ms(value)
        return value

    @model_validator(mode="after")
    def reject_null_punched_at(self) -> PunchInRequest:
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at must be an epoch-millisecond integer")
        return self


class PunchOutRequest(InputModel):
    emp_code: str
    punched_at: EpochMillis = Field(default=None, json_schema_extra=omit_null_schema_default)

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, value: int | None) -> int | None:
        if value is not None:
            validate_epoch_ms(value)
        return value

    @model_validator(mode="after")
    def reject_null_punched_at(self) -> PunchOutRequest:
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at must be an epoch-millisecond integer")
        return self


class RegularizeRequest(InputModel):
    status: AttendanceStatus = Field(default=None, json_schema_extra=omit_null_schema_default)
    punch_in: EpochMillis = Field(default=None, json_schema_extra=omit_null_schema_default)
    punch_out: EpochMillis = Field(default=None, json_schema_extra=omit_null_schema_default)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def validate_supplied_timestamps(self) -> RegularizeRequest:
        for field_name in ("punch_in", "punch_out"):
            if field_name in self.model_fields_set:
                value = getattr(self, field_name)
                if value is None:
                    raise ValueError(f"{field_name} must be an epoch-millisecond integer")
                validate_epoch_ms(value)
        if "status" in self.model_fields_set and self.status is None:
            raise ValueError("status cannot be null")
        return self


class HealthResponse(BaseModel):
    status: Literal["ok"]


class ErrorResponse(BaseModel):
    detail: str


class EmployeeResponse(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: str = Field(json_schema_extra={"format": "date"})
    created_at: EpochMillis = Field(json_schema_extra={"readOnly": True})


class EmployeePageResponse(BaseModel):
    items: list[EmployeeResponse]
    total: int
    page: int
    page_size: int


class FieldChangeResponse(BaseModel):
    from_value: Any = Field(alias="from")
    to: Any


class HistoryEntryResponse(BaseModel):
    at: EpochMillis
    by: str
    reason: str
    changes: dict[str, FieldChangeResponse]


class AttendanceResponse(BaseModel):
    emp_code: str
    date: str = Field(json_schema_extra={"format": "date"})
    status: AttendanceStatus
    punch_in: EpochMillis | None
    punch_out: EpochMillis | None
    work_hours: float | None = Field(json_schema_extra={"readOnly": True})
    late_minutes: int = Field(json_schema_extra={"readOnly": True})
    overtime_minutes: int = Field(json_schema_extra={"readOnly": True})
    half_day: bool = Field(json_schema_extra={"readOnly": True})
    history: list[HistoryEntryResponse]


class AttendancePageResponse(BaseModel):
    items: list[AttendanceResponse]
    total: int
    page: int
    page_size: int


class EmployeeMonthlyResponse(BaseModel):
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: float | None


class DepartmentSummaryItem(BaseModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: float | None
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int


class DepartmentSummaryResponse(BaseModel):
    month: str
    items: list[DepartmentSummaryItem]


class LeaderboardItem(BaseModel):
    rank: int
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int


class LeaderboardResponse(BaseModel):
    month: str
    items: list[LeaderboardItem]


class TrendItem(BaseModel):
    date: str = Field(json_schema_extra={"format": "date"})
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: float | None
    moving_avg_7d: float | None


class TrendResponse(BaseModel):
    department: str
    items: list[TrendItem]


class ExplainResponse(BaseModel):
    endpoint: str
    collection: str
    explain: dict[str, Any]


@app.exception_handler(PyMongoError)
async def handle_database_error(_: Request, _exc: PyMongoError) -> JSONResponse:
    logger.error("MongoDB request failed (%s)", type(_exc).__name__)
    return JSONResponse(status_code=503, content={"detail": "Database unavailable"})


def parse_date(value: str) -> date:
    if not DATE_PATTERN.fullmatch(value):
        raise ValueError("date must use YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("date must be a valid calendar date") from exc


def parse_month(value: str) -> tuple[date, date]:
    if not MONTH_PATTERN.fullmatch(value):
        raise HTTPException(422, "month must use YYYY-MM")
    year, month = map(int, value.split("-"))
    start = date(year, month, 1)
    next_month = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    return start, next_month - timedelta(days=1)


def validate_epoch_ms(value: int) -> None:
    if not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
        raise ValueError(f"timestamp must be between {MIN_EPOCH_MS} and {MAX_EPOCH_MS} epoch milliseconds")


def epoch_ms_to_datetime(value: int) -> datetime:
    validate_epoch_ms(value)
    return datetime.fromtimestamp(value // 1000, UTC)


def datetime_to_epoch_ms(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp()) * 1000


def now_utc_seconds() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _shift_time(value: str) -> time:
    hour, minute = map(int, value.split(":"))
    return time(hour, minute)


def attendance_date_for(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    local = punch_in.astimezone(IST)
    local_date = local.date()
    if _shift_time(shift_end) <= _shift_time(shift_start) and local.time().replace(tzinfo=None) < _shift_time(shift_end):
        return local_date - timedelta(days=1)
    return local_date


def compute_late_minutes(punch_in: datetime, shift_start: str, attendance_day: date) -> int:
    local_start = datetime.combine(attendance_day, _shift_time(shift_start), IST)
    elapsed_seconds = int((punch_in.astimezone(IST) - local_start).total_seconds())
    return elapsed_seconds // 60 if elapsed_seconds > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((punch_out - punch_in).total_seconds())
    hours = (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, attendance_day: date) -> int:
    end_day = attendance_day
    if _shift_time(shift_end) <= _shift_time(shift_start):
        end_day += timedelta(days=1)
    shift_end_at = datetime.combine(end_day, _shift_time(shift_end), IST)
    elapsed_seconds = int((punch_out.astimezone(IST) - shift_end_at).total_seconds())
    minutes = elapsed_seconds // 60
    return minutes if minutes >= 30 else 0


def employee_to_api(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "emp_code": document["emp_code"],
        "name": document["name"],
        "email": document["email"],
        "department": document["department"],
        "shift_start": document["shift_start"],
        "shift_end": document["shift_end"],
        "joined_on": document["joined_on"],
        "created_at": datetime_to_epoch_ms(document["created_at"]),
    }


def _history_to_api(history: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    result = []
    for entry in history or []:
        changes: dict[str, Any] = {}
        for field_name, pair in entry.get("changes", {}).items():
            changes[field_name] = dict(pair)
            if field_name in ("punch_in", "punch_out"):
                changes[field_name]["from"] = datetime_to_epoch_ms(pair.get("from"))
                changes[field_name]["to"] = datetime_to_epoch_ms(pair.get("to"))
        result.append({
            "at": datetime_to_epoch_ms(entry["at"]),
            "by": entry["by"],
            "reason": entry["reason"],
            "changes": changes,
        })
    return result


def attendance_to_api(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "emp_code": document["emp_code"],
        "date": document["date"],
        "status": document["status"],
        "punch_in": datetime_to_epoch_ms(document.get("punch_in")),
        "punch_out": datetime_to_epoch_ms(document.get("punch_out")),
        "work_hours": document.get("work_hours"),
        "late_minutes": document.get("late_minutes", 0),
        "overtime_minutes": document.get("overtime_minutes", 0),
        "half_day": document.get("half_day", False),
        "history": _history_to_api(document.get("history", [])),
    }


def ensure_indexes(database: Any) -> None:
    database.employees.create_index([("emp_code", ASCENDING)], unique=True, name="employee_code_unique")
    database.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)], name="employee_department_code"
    )
    database.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)], name="employee_department_joined"
    )
    database.employees.create_index(
        [("joined_on", ASCENDING), ("department", ASCENDING), ("emp_code", ASCENDING)],
        name="employee_joined_department",
    )
    database.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="attendance_employee_date_unique"
    )
    database.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_employee"
    )
    database.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_status_date_employee"
    )
    database.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="attendance_employee_punch_in"
    )


def initialize_database() -> None:
    if db is None:
        return
    try:
        client.admin.command("ping")
        ensure_indexes(db)
    except PyMongoError as exc:
        logger.error("MongoDB startup initialization failed (%s)", type(exc).__name__)
        return


@app.get(
    "/health", tags=["system"], summary="Readiness probe", operation_id="health",
    response_model=HealthResponse, responses={503: {"model": ErrorResponse}},
)
def health() -> dict[str, str]:
    if db is None or client is None:
        raise HTTPException(503, "Database unavailable")
    try:
        client.admin.command("ping")
    except PyMongoError as exc:
        raise HTTPException(503, "Database unavailable") from exc
    return {"status": "ok"}


@app.post(
    "/employees", status_code=201, tags=["employees"], summary="Create an employee",
    operation_id="createEmployee", response_model=EmployeeResponse,
    responses={409: {"model": ErrorResponse}},
)
def create_employee(body: EmployeeCreate) -> dict[str, Any]:
    document = body.model_dump()
    document["created_at"] = now_utc_seconds()
    try:
        db.employees.insert_one(document)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "emp_code already exists") from exc
    return employee_to_api(document)


@app.get(
    "/employees", tags=["employees"], summary="List employees",
    operation_id="listEmployees", response_model=EmployeePageResponse,
)
def list_employees(
    department: str = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    documents = db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING)
    documents = documents.skip((page - 1) * page_size).limit(page_size)
    return {
        "items": [employee_to_api(document) for document in documents],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post(
    "/attendance/punch-in", status_code=201, tags=["attendance"], summary="Record a punch-in",
    operation_id="punchIn", response_model=AttendanceResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
def punch_in(body: PunchInRequest) -> dict[str, Any]:
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    punched_at = epoch_ms_to_datetime(body.punched_at) if body.punched_at is not None else now_utc_seconds()
    attendance_day = attendance_date_for(punched_at, employee["shift_start"], employee["shift_end"])
    document = {
        "emp_code": body.emp_code,
        "date": attendance_day.isoformat(),
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(punched_at, employee["shift_start"], attendance_day),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(document)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "already punched in for this date") from exc
    return attendance_to_api(document)


def _attendance_query(
    emp_code: str | None,
    date_from: str | None,
    date_to: str | None,
    status: str | None,
) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        bounds: dict[str, str] = {}
        if date_from is not None:
            try:
                parse_date(date_from)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            bounds["$gte"] = date_from
        if date_to is not None:
            try:
                parse_date(date_to)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            bounds["$lte"] = date_to
        if date_from is not None and date_to is not None and date_from > date_to:
            raise HTTPException(422, "date_from must be on or before date_to")
        query["date"] = bounds
    if status is not None:
        if status not in ALL_STATUSES:
            raise HTTPException(422, "invalid status")
        query["status"] = status
    return query


def _attendance_sort() -> list[tuple[str, int]]:
    return [("date", DESCENDING), ("emp_code", ASCENDING)]


@app.get(
    "/attendance", tags=["attendance"], summary="List attendance records",
    operation_id="listAttendance", response_model=AttendancePageResponse,
)
def list_attendance(
    emp_code: str = Query(None),
    date_from: str = Query(None, json_schema_extra={"format": "date"}),
    date_to: str = Query(None, json_schema_extra={"format": "date"}),
    status: AttendanceStatus = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    query = _attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(query)
    cursor = db.attendance_logs.find(query).sort(_attendance_sort())
    documents = cursor.skip((page - 1) * page_size).limit(page_size)
    return {
        "items": [attendance_to_api(document) for document in documents],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post(
    "/attendance/punch-out", status_code=200, tags=["attendance"], summary="Record a punch-out",
    operation_id="punchOut", response_model=AttendanceResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
def punch_out(body: PunchOutRequest) -> dict[str, Any]:
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    punched_at = epoch_ms_to_datetime(body.punched_at) if body.punched_at is not None else now_utc_seconds()
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}},
        sort=[("punch_in", DESCENDING), ("date", DESCENDING)],
    )
    if record is None:
        raise HTTPException(404, "no punch-in found")
    punch_in_at = record["punch_in"]
    elapsed = punched_at - punch_in_at
    if elapsed.total_seconds() <= 0 or elapsed > timedelta(hours=24):
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "record is already punched out")
    attendance_day = parse_date(record["date"])
    values = {
        "punch_out": punched_at,
        "work_hours": compute_work_hours(punch_in_at, punched_at),
        "overtime_minutes": compute_overtime(
            punched_at, employee["shift_start"], employee["shift_end"], attendance_day
        ),
    }
    values["half_day"] = values["work_hours"] < 4.50
    updated = db.attendance_logs.find_one_and_update(
        {"emp_code": body.emp_code, "date": record["date"], "punch_in": punch_in_at, "punch_out": None},
        {"$set": values},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record is already punched out")
    return attendance_to_api(updated)


def _same_value(left: Any, right: Any) -> bool:
    if isinstance(left, datetime) and isinstance(right, datetime):
        if left.tzinfo is None:
            left = left.replace(tzinfo=UTC)
        if right.tzinfo is None:
            right = right.replace(tzinfo=UTC)
        return left == right
    return left == right


@app.patch(
    "/attendance/{emp_code}/{date}", tags=["attendance"], summary="Regularize attendance",
    operation_id="regularizeAttendance", response_model=AttendanceResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
def regularize_attendance(
    emp_code: str,
    date: Annotated[str, Path(json_schema_extra={"format": "date"})],
    body: RegularizeRequest,
) -> dict[str, Any]:
    try:
        attendance_day = parse_date(date)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    current = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if current is None:
        raise HTTPException(404, "attendance record not found")

    supplied = body.model_fields_set
    final_status = body.status if "status" in supplied else current["status"]
    if final_status in ("ABSENT", "LEAVE") and ("punch_in" in supplied or "punch_out" in supplied):
        raise HTTPException(422, "ABSENT and LEAVE records cannot include punch times")
    final_punch_in = (
        epoch_ms_to_datetime(body.punch_in) if "punch_in" in supplied else current.get("punch_in")
    )
    final_punch_out = (
        epoch_ms_to_datetime(body.punch_out) if "punch_out" in supplied else current.get("punch_out")
    )
    if final_status in ("ABSENT", "LEAVE"):
        final_punch_in = None
        final_punch_out = None
    elif final_punch_in is None:
        raise HTTPException(422, "presence statuses require punch_in")
    elif attendance_date_for(final_punch_in, employee["shift_start"], employee["shift_end"]) != attendance_day:
        raise HTTPException(422, "punch_in must belong to the record attendance date")

    if final_punch_out is not None:
        if final_punch_in is None:
            raise HTTPException(422, "punch_out requires punch_in")
        elapsed = final_punch_out - final_punch_in
        if elapsed.total_seconds() <= 0 or elapsed > timedelta(hours=24):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    if final_status in ("ABSENT", "LEAVE"):
        derived: dict[str, Any] = {
            "late_minutes": 0,
            "work_hours": None,
            "overtime_minutes": 0,
            "half_day": False,
        }
    else:
        derived = {
            "late_minutes": compute_late_minutes(final_punch_in, employee["shift_start"], attendance_day),
            "work_hours": compute_work_hours(final_punch_in, final_punch_out) if final_punch_out else None,
            "overtime_minutes": (
                compute_overtime(final_punch_out, employee["shift_start"], employee["shift_end"], attendance_day)
                if final_punch_out
                else 0
            ),
            "half_day": bool(final_punch_out and compute_work_hours(final_punch_in, final_punch_out) < 4.50),
        }

    final_values = {
        "status": final_status,
        "punch_in": final_punch_in,
        "punch_out": final_punch_out,
        **derived,
    }
    previous_values = {
        "status": current["status"],
        "punch_in": current.get("punch_in"),
        "punch_out": current.get("punch_out"),
        "work_hours": current.get("work_hours"),
        "late_minutes": current.get("late_minutes", 0),
        "overtime_minutes": current.get("overtime_minutes", 0),
        "half_day": current.get("half_day", False),
    }
    changes = {
        key: {"from": previous_values[key], "to": final_values[key]}
        for key in final_values
        if not _same_value(previous_values[key], final_values[key])
    }
    if not changes:
        raise HTTPException(422, "request does not change the attendance record")

    history_entry = {
        "at": now_utc_seconds(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    selector: dict[str, Any] = {"_id": current["_id"], "emp_code": emp_code, "date": date}
    for field_name, value in previous_values.items():
        selector[field_name] = value if field_name in current else {"$exists": False}
    selector["history"] = current["history"] if "history" in current else {"$exists": False}
    updated = db.attendance_logs.find_one_and_update(
        selector,
        {"$set": final_values, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "attendance record changed concurrently")
    return attendance_to_api(updated)


def _month_bounds(month: str) -> tuple[date, date]:
    return parse_month(month)


def _date_string(day: date) -> str:
    return day.isoformat()


def _month_record_match(month_start: date, month_end: date) -> dict[str, Any]:
    return {"$gte": _date_string(month_start), "$lte": _date_string(month_end)}


def _weekday_expr(date_field: str = "$date") -> dict[str, Any]:
    return {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": date_field}}}, [2, 3, 4, 5, 6]]}


def _rounded_positive_expr(expression: Any, places: int) -> dict[str, Any]:
    scale = Decimal128(str(10**places))
    half = Decimal128("0.5")
    return {
        "$toDouble": {
            "$divide": [
                {"$floor": {"$add": [{"$multiply": [{"$toDecimal": expression}, scale]}, half]}},
                scale,
            ]
        }
    }


def employee_monthly_pipeline(emp_code: str, month_start: date, month_end: date) -> list[dict[str, Any]]:
    start_text, end_text = _date_string(month_start), _date_string(month_end)
    first_day = {"$dateFromString": {"dateString": start_text}}
    last_day = {"$dateFromString": {"dateString": end_text}}
    joined_day = {"$dateFromString": {"dateString": "$joined_on"}}
    working_start = {"$max": [joined_day, first_day]}
    day_count = {"$dateDiff": {"startDate": working_start, "endDate": last_day, "unit": "day"}}
    working_days = {
        "$cond": [
            {"$lte": [working_start, last_day]},
            {
                "$size": {
                    "$filter": {
                        "input": {
                            "$map": {
                                "input": {"$range": [0, {"$add": [day_count, 1]}]},
                                "as": "offset",
                                "in": {"$dateAdd": {"startDate": working_start, "unit": "day", "amount": "$$offset"}},
                            }
                        },
                        "as": "work_date",
                        "cond": {"$in": [{"$dayOfWeek": "$$work_date"}, [2, 3, 4, 5, 6]]},
                    }
                }
            },
            0,
        ]
    }
    return [
        {"$match": {"emp_code": emp_code}},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"employee_code": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$employee_code"]},
                    {"$gte": ["$date", start_text]},
                    {"$lte": ["$date", end_text]},
                ]}}},
                {"$group": {
                    "_id": None,
                    "present_days": {"$sum": {"$cond": [
                        {"$and": [{"$in": ["$status", list(PRESENCE_STATUSES)]}, _weekday_expr()]},
                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                        0,
                    ]}},
                    "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
                }},
            ],
            "as": "summary",
        }},
        {"$unwind": {"path": "$summary", "preserveNullAndEmptyArrays": True}},
        {"$set": {"working_days": working_days}},
        {"$project": {
            "_id": 0,
            "emp_code": 1,
            "month": {"$literal": f"{month_start:%Y-%m}"},
            "working_days": 1,
            "present_days": {"$ifNull": ["$summary.present_days", 0]},
            "leave_days": {"$ifNull": ["$summary.leave_days", 0]},
            "late_count": {"$ifNull": ["$summary.late_count", 0]},
            "total_late_minutes": {"$ifNull": ["$summary.total_late_minutes", 0]},
            "total_overtime_minutes": {"$ifNull": ["$summary.total_overtime_minutes", 0]},
        }},
        {"$set": {"attendance_pct": {"$cond": [
            {"$gt": ["$working_days", 0]},
            _rounded_positive_expr({"$multiply": [{"$divide": ["$present_days", "$working_days"]}, 100]}, 2),
            None,
        ]}}},
    ]


def department_summary_pipeline(month_start: date, month_end: date, department: str | None) -> list[dict[str, Any]]:
    employee_match: dict[str, Any] = {"joined_on": {"$lte": _date_string(month_end)}}
    if department is not None:
        employee_match["department"] = department
    start_text, end_text = _date_string(month_start), _date_string(month_end)
    return [
        {"$match": employee_match},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"employee_code": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$employee_code"]},
                    {"$gte": ["$date", start_text]},
                    {"$lte": ["$date", end_text]},
                ]}}},
                {"$group": {
                    "_id": None,
                    "present_days": {"$sum": {"$cond": [
                        {"$and": [{"$in": ["$status", list(PRESENCE_STATUSES)]}, _weekday_expr()]},
                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                        0,
                    ]}},
                    "work_hours_sum": {"$sum": {"$cond": [
                        {"$and": [
                            {"$in": ["$status", list(PRESENCE_STATUSES)]},
                            {"$ne": [{"$ifNull": ["$work_hours", None]}, None]},
                        ]}, "$work_hours", 0,
                    ]}},
                    "work_hours_count": {"$sum": {"$cond": [{"$and": [
                        {"$in": ["$status", list(PRESENCE_STATUSES)]},
                        {"$ne": [{"$ifNull": ["$work_hours", None]}, None]},
                    ]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                }},
            ],
            "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": {"$ifNull": ["$logs.present_days", 0]}},
            "work_hours_sum": {"$sum": {"$ifNull": ["$logs.work_hours_sum", 0]}},
            "work_hours_count": {"$sum": {"$ifNull": ["$logs.work_hours_count", 0]}},
            "late_count": {"$sum": {"$ifNull": ["$logs.late_count", 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.total_late_minutes", 0]}},
            "leave_count": {"$sum": {"$ifNull": ["$logs.leave_count", 0]}},
            "on_duty_count": {"$sum": {"$ifNull": ["$logs.on_duty_count", 0]}},
        }},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": 1,
            "present_days": 1,
            "avg_work_hours": {"$cond": [
                {"$gt": ["$work_hours_count", 0]},
                _rounded_positive_expr({"$divide": ["$work_hours_sum", "$work_hours_count"]}, 2),
                None,
            ]},
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
        }},
        {"$sort": {"department": 1}},
    ]


def late_leaderboard_pipeline(
    month_start: date, month_end: date, limit: int, department: str | None
) -> list[dict[str, Any]]:
    pipeline: list[dict[str, Any]] = [
        {"$match": {"date": _month_record_match(month_start, month_end), "late_minutes": {"$gt": 0}}},
        {"$group": {
            "_id": "$emp_code",
            "emp_code": {"$first": "$emp_code"},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1},
        }},
        {"$lookup": {
            "from": "employees",
            "let": {"employee_code": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$eq": ["$emp_code", "$$employee_code"]}}},
                {"$project": {"_id": 0, "name": 1, "department": 1}},
            ],
            "as": "employee",
        }},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": 1,
            "name": "$employee.name",
            "department": "$employee.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ])
    return pipeline


def department_trend_pipeline(department: str, start: date, end: date) -> list[dict[str, Any]]:
    start_dt = datetime.combine(start, time.min, UTC)
    end_dt = datetime.combine(end, time.min, UTC)
    return [
        {"$match": {"department": department}},
        {"$project": {"_id": 0, "emp_code": 1, "joined_on": 1}},
        {"$group": {"_id": None, "employees": {"$push": {"emp_code": "$emp_code", "joined_on": "$joined_on"}}}},
        {"$set": {
            "trend_dates": {"$map": {
                "input": {"$range": [0, {"$add": [
                    {"$dateDiff": {"startDate": {"$literal": start_dt}, "endDate": {"$literal": end_dt}, "unit": "day"}}, 1,
                ]}]},
                "as": "offset",
                "in": {"$dateAdd": {"startDate": {"$literal": start_dt}, "unit": "day", "amount": "$$offset"}},
            }}
        }},
        {"$unwind": "$trend_dates"},
        {"$set": {"trend_day": {"$dateToString": {"date": "$trend_dates", "format": "%Y-%m-%d", "timezone": "UTC"}}}},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {
                "trend_day": "$trend_day",
                "employee_codes": {"$map": {
                    "input": "$employees",
                    "as": "employee",
                    "in": "$$employee.emp_code",
                }},
            },
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$date", "$$trend_day"]},
                    {"$in": ["$emp_code", "$$employee_codes"]},
                ]}}},
                {"$group": {
                    "_id": None,
                    "present_count": {"$sum": {"$cond": [
                        {"$in": ["$status", list(PRESENCE_STATUSES)]},
                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                        0,
                    ]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                }},
            ],
            "as": "daily_logs",
        }},
        {"$unwind": {"path": "$daily_logs", "preserveNullAndEmptyArrays": True}},
        {"$set": {
            "headcount": {"$size": {"$filter": {
                "input": "$employees",
                "as": "employee",
                "cond": {"$lte": ["$$employee.joined_on", "$trend_day"]},
            }}},
            "is_working_day": {"$in": [{"$dayOfWeek": "$trend_dates"}, [2, 3, 4, 5, 6]]},
            "present_count": {"$ifNull": ["$daily_logs.present_count", 0]},
            "late_count": {"$ifNull": ["$daily_logs.late_count", 0]},
        }},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            _rounded_positive_expr({"$divide": ["$present_count", "$headcount"]}, 4),
            None,
        ]}}},
        {"$setWindowFields": {
            "sortBy": {"trend_dates": 1},
            "output": {"moving_avg_raw": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
        }},
        {"$project": {
            "_id": 0,
            "date": {"$dateToString": {"date": "$trend_dates", "format": "%Y-%m-%d", "timezone": "UTC"}},
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": {"$cond": [
                {"$ne": ["$moving_avg_raw", None]},
                _rounded_positive_expr("$moving_avg_raw", 4),
                None,
            ]},
        }},
        {"$sort": {"date": 1}},
    ]


@app.get(
    "/analytics/employees/{emp_code}/monthly", tags=["analytics"],
    summary="Monthly attendance summary for one employee", operation_id="employeeMonthly",
    response_model=EmployeeMonthlyResponse,
    responses={404: {"model": ErrorResponse}},
)
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")) -> dict[str, Any]:
    month_start, month_end = _month_bounds(month)
    result = next(db.employees.aggregate(employee_monthly_pipeline(emp_code, month_start, month_end)), None)
    if result is None:
        raise HTTPException(404, "employee not found")
    return result


@app.get(
    "/analytics/departments/summary", tags=["analytics"], summary="Department summary for a month",
    operation_id="departmentSummary", response_model=DepartmentSummaryResponse,
)
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: str = Query(None),
) -> dict[str, Any]:
    month_start, month_end = _month_bounds(month)
    return {
        "month": month,
        "items": list(db.employees.aggregate(department_summary_pipeline(month_start, month_end, department))),
    }


@app.get(
    "/analytics/leaderboard/late", tags=["analytics"], summary="Late-comers leaderboard for a month",
    operation_id="lateLeaderboard", response_model=LeaderboardResponse,
)
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: str = Query(None),
) -> dict[str, Any]:
    month_start, month_end = _month_bounds(month)
    return {
        "month": month,
        "items": list(db.attendance_logs.aggregate(
            late_leaderboard_pipeline(month_start, month_end, limit, department)
        )),
    }


@app.get(
    "/analytics/departments/{department}/trend", tags=["analytics"],
    summary="Daily attendance trend for a department", operation_id="departmentTrend",
    response_model=TrendResponse,
    responses={404: {"model": ErrorResponse}},
)
def department_trend(
    department: str,
    date_from: str = Query(..., alias="from", json_schema_extra={"format": "date"}),
    date_to: str = Query(..., alias="to", json_schema_extra={"format": "date"}),
) -> dict[str, Any]:
    try:
        start, end = parse_date(date_from), parse_date(date_to)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if end < start:
        raise HTTPException(422, "to must be on or after from")
    if (end - start).days + 1 > 92:
        raise HTTPException(422, "date range cannot exceed 92 days")
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "department not found")
    return {"department": department, "items": list(db.employees.aggregate(
        department_trend_pipeline(department, start, end)
    ))}


def _explain_find(query: dict[str, Any], page: int, page_size: int) -> dict[str, Any]:
    command = {
        "find": "attendance_logs",
        "filter": query,
        "sort": dict(_attendance_sort()),
        "skip": (page - 1) * page_size,
        "limit": page_size,
    }
    return db.command("explain", command, verbosity="executionStats")


def _explain_aggregate(collection: Any, pipeline: list[dict[str, Any]]) -> dict[str, Any]:
    return collection.database.command(
        "explain",
        {"aggregate": collection.name, "pipeline": pipeline, "cursor": {}},
        verbosity="executionStats",
    )


@app.get(
    "/admin/explain/{endpoint}", tags=["admin"], summary="MongoDB explain for an endpoint's main query",
    operation_id="explainEndpoint", response_model=ExplainResponse,
)
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: str = Query(None),
    month: str = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: str = Query(None),
    limit: int = Query(10, ge=1, le=50),
    date_from: str = Query(None, json_schema_extra={"format": "date"}),
    date_to: str = Query(None, json_schema_extra={"format": "date"}),
    status: AttendanceStatus = Query(None),
    date_start: str = Query(None, alias="from", json_schema_extra={"format": "date"}),
    date_end: str = Query(None, alias="to", json_schema_extra={"format": "date"}),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    if endpoint == "attendance_list":
        query = _attendance_query(emp_code, date_from, date_to, status)
        explain = _explain_find(query, page, page_size)
        collection_name = "attendance_logs"
    elif endpoint == "employee_monthly":
        if emp_code is None or month is None:
            raise HTTPException(422, "emp_code and month are required")
        month_start, month_end = _month_bounds(month)
        pipeline = employee_monthly_pipeline(emp_code, month_start, month_end)
        explain = _explain_aggregate(db.employees, pipeline)
        collection_name = "employees"
    elif endpoint == "department_summary":
        if month is None:
            raise HTTPException(422, "month is required")
        month_start, month_end = _month_bounds(month)
        pipeline = department_summary_pipeline(month_start, month_end, department)
        explain = _explain_aggregate(db.employees, pipeline)
        collection_name = "employees"
    elif endpoint == "late_leaderboard":
        if month is None:
            raise HTTPException(422, "month is required")
        month_start, month_end = _month_bounds(month)
        pipeline = late_leaderboard_pipeline(month_start, month_end, limit, department)
        explain = _explain_aggregate(db.attendance_logs, pipeline)
        collection_name = "attendance_logs"
    else:
        if department is None or date_start is None or date_end is None:
            raise HTTPException(422, "department, from and to are required")
        try:
            start, end = parse_date(date_start), parse_date(date_end)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if end < start or (end - start).days + 1 > 92:
            raise HTTPException(422, "invalid trend date range")
        pipeline = department_trend_pipeline(department, start, end)
        explain = _explain_aggregate(db.employees, pipeline)
        collection_name = "employees"
    serializable_explain = json.loads(json_util.dumps(explain))
    return {"endpoint": endpoint, "collection": collection_name, "explain": serializable_explain}
