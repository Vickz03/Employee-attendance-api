"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import calendar
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, status
from pydantic import BaseModel, Field, field_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

# Centralized MongoDB client with connection pooling and server selection timeout
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client: MongoClient = MongoClient(
    MONGO_URI,
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
    connectTimeoutMS=5000,
)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


# --------------------------------------------------------------------------- #
# Indexes creation (Startup lifecycle)
# --------------------------------------------------------------------------- #
def init_indexes():
    """Create all required database indexes safely and idempotently."""
    try:
        # Unique customer-generated employee code
        db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="idx_employees_emp_code_unique")
        # Department filtering on employees
        db.employees.create_index([("department", ASCENDING)], name="idx_employees_department")
        # Joined_on for headcount queries
        db.employees.create_index([("joined_on", ASCENDING)], name="idx_employees_joined_on")

        # Unique compound index: at most one attendance record per (emp_code, date)
        db.attendance_logs.create_index(
            [("emp_code", ASCENDING), ("date", ASCENDING)],
            unique=True,
            name="idx_attendance_emp_code_date_unique",
        )
        # Attendance listing, queries and analytics
        db.attendance_logs.create_index(
            [("date", DESCENDING), ("emp_code", ASCENDING)],
            name="idx_attendance_date_desc_emp_asc",
        )
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", DESCENDING)], name="idx_attendance_emp_date")
        db.attendance_logs.create_index([("status", ASCENDING)], name="idx_attendance_status")
        db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", ASCENDING)], name="idx_attendance_date_late")
    except Exception as e:
        # Log index initialization error without crashing startup
        print(f"Index initialization warning: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_indexes()
    yield
    client.close()


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Pure Helper Functions (Business Rules R1 - R10)
# --------------------------------------------------------------------------- #
def round_half_up(val: float | Decimal | None, decimals: int = 2) -> float | None:
    """R8: 2 decimals half-up (rates: 4 decimals half-up)."""
    if val is None:
        return None
    d = Decimal(str(val))
    target = Decimal("10") ** -decimals
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def instant_to_epoch_ms(dt: datetime | None) -> int | None:
    """Converts a BSON/UTC datetime to integer epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def epoch_ms_to_datetime(ms: int | None) -> datetime | None:
    """
    R1: Instants are truncated to whole seconds before any calculation,
    and the truncated value is stored and returned.
    """
    if ms is None:
        return None
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def get_ist_datetime(dt: datetime) -> datetime:
    """Converts a UTC datetime to IST."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def derive_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: A record's date is the IST calendar date of its punch_in instant.
    Overnight shifts (shift_end <= shift_start, e.g. 22:00 - 06:00):
    a punch-in earlier than shift_end belongs to the shift that started the previous day.
    """
    punch_ist = get_ist_datetime(punch_in_utc)
    if shift_end <= shift_start:
        end_h, end_m = map(int, shift_end.split(":"))
        if (punch_ist.hour, punch_ist.minute, punch_ist.second) < (end_h, end_m, 0):
            prev_day = punch_ist.date() - timedelta(days=1)
            return prev_day.isoformat()
    return punch_ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime | None, shift_start: str, attendance_date: str) -> int:
    """
    R2: Grace period 10 minutes. Late only if punch-in is strictly more than 10:00 after shift_start;
    then late_minutes = whole minutes (floor) elapsed since shift_start.
    """
    if punch_in_utc is None:
        return 0
    punch_ist = get_ist_datetime(punch_in_utc)
    y, m, d = map(int, attendance_date.split("-"))
    sh, sm = map(int, shift_start.split(":"))
    shift_start_ist = datetime(y, m, d, sh, sm, 0, tzinfo=IST)

    elapsed_sec = (punch_ist - shift_start_ist).total_seconds()
    if elapsed_sec > 600:
        return int(elapsed_sec // 60)
    return 0


def compute_overtime_minutes(
    punch_out_utc: datetime | None, shift_start: str, shift_end: str, attendance_date: str
) -> int:
    """
    R3: Whole minutes (floor) between shift_end and punch_out, counted only when >= 30; otherwise 0.
    For overnight shifts (shift_end <= shift_start), shift end is on the next calendar day.
    """
    if punch_out_utc is None:
        return 0
    punch_ist = get_ist_datetime(punch_out_utc)
    y, m, d = map(int, attendance_date.split("-"))
    eh, em = map(int, shift_end.split(":"))
    shift_end_ist = datetime(y, m, d, eh, em, 0, tzinfo=IST)
    if shift_end <= shift_start:
        shift_end_ist += timedelta(days=1)

    elapsed_sec = (punch_ist - shift_end_ist).total_seconds()
    if elapsed_sec >= 1800:
        return int(elapsed_sec // 60)
    return 0


def compute_work_hours(punch_in_utc: datetime | None, punch_out_utc: datetime | None) -> float | None:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up. Null until punch-out.
    """
    if punch_in_utc is None or punch_out_utc is None:
        return None
    sec = (punch_out_utc - punch_in_utc).total_seconds()
    hours = sec / 3600.0
    return round_half_up(hours, 2)


def compute_half_day(work_hours: float | None) -> bool:
    """
    R5: After punch-out, half_day = true when rounded work_hours < 4.50.
    """
    if work_hours is None:
        return False
    return work_hours < 4.50


def serialize_history_entry(entry: dict) -> dict:
    changes = {}
    raw_changes = entry.get("changes", {})
    for k, v in raw_changes.items():
        from_val = v.get("from")
        to_val = v.get("to")
        if isinstance(from_val, datetime):
            from_val = instant_to_epoch_ms(from_val)
        if isinstance(to_val, datetime):
            to_val = instant_to_epoch_ms(to_val)
        changes[k] = {"from": from_val, "to": to_val}
    return {
        "at": instant_to_epoch_ms(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes,
    }


def serialize_attendance_doc(doc: dict) -> dict:
    history = [serialize_history_entry(h) for h in doc.get("history", [])]
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": instant_to_epoch_ms(doc.get("punch_in")),
        "punch_out": instant_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history,
    }


def serialize_employee_doc(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": instant_to_epoch_ms(doc.get("created_at")),
    }


# --------------------------------------------------------------------------- #
# Request Models
# --------------------------------------------------------------------------- #
EPOCH_MS_MIN = 100_000_000_000
EPOCH_MS_MAX = 4_102_444_800_000


class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("shift_end")
    @classmethod
    def validate_shifts(cls, v: str, info):
        start = info.data.get("shift_start")
        if start and v == start:
            raise ValueError("shift_start must differ from shift_end")
        return v

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str):
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError("joined_on must be a valid YYYY-MM-DD calendar date")
        return v


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=EPOCH_MS_MIN, le=EPOCH_MS_MAX)
    status: str = Field("PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=EPOCH_MS_MIN, le=EPOCH_MS_MAX)


class RegularizeIn(BaseModel):
    status: Optional[str] = Field(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = Field(None, ge=EPOCH_MS_MIN, le=EPOCH_MS_MAX)
    punch_out: Optional[int] = Field(None, ge=EPOCH_MS_MIN, le=EPOCH_MS_MAX)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    """Returns 200 only if MongoDB answers a ping, otherwise 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Database unavailable: {str(e)}",
        )


@app.post("/employees", status_code=status.HTTP_201_CREATED)
def create_employee(body: EmployeeIn):
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = now_utc

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="emp_code already exists")

    return serialize_employee_doc(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [serialize_employee_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=status.HTTP_201_CREATED)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_dt = epoch_ms_to_datetime(body.punched_at)
    else:
        # Truncate current instant to whole seconds
        now = datetime.now(timezone.utc)
        punch_in_dt = now.replace(microsecond=0)

    att_date = derive_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(punch_in_dt, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="already punched in for this date",
        )

    return serialize_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=status.HTTP_200_OK)
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_dt = epoch_ms_to_datetime(body.punched_at)
    else:
        now = datetime.now(timezone.utc)
        punch_out_dt = now.replace(microsecond=0)

    # Find the most recent record whose punch_in is <= punched_at
    candidate = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}},
        sort=[("punch_in", DESCENDING)],
    )

    if not candidate:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no punch-in found")

    punch_in_dt = candidate.get("punch_in")
    if not punch_in_dt:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no punch-in found")

    # If already punched out
    if candidate.get("punch_out") is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="that record is already punched out",
        )

    # Validate timing: punched_at <= punch_in or > 24 hours
    elapsed_sec = (punch_out_dt - punch_in_dt).total_seconds()
    if elapsed_sec <= 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="punch_out must be strictly greater than punch_in",
        )
    if elapsed_sec > 86400:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="punch_out cannot be more than 24 hours after punch_in",
        )

    work_hrs = compute_work_hours(punch_in_dt, punch_out_dt)
    ot_min = compute_overtime_minutes(punch_out_dt, emp["shift_start"], emp["shift_end"], candidate["date"])
    half_d = compute_half_day(work_hrs)

    # Atomic state transition: update only if punch_out is STILL None
    updated = db.attendance_logs.find_one_and_update(
        {"_id": candidate["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hrs,
                "overtime_minutes": ot_min,
                "half_day": half_d,
            }
        },
        return_document=True,
    )

    if not updated:
        # Another concurrent request punched out first
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="that record is already punched out",
        )

    return serialize_attendance_doc(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status_filter: Optional[str] = Query(None, alias="status", pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="date_from cannot be greater than date_to",
        )

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status_filter:
        q["status"] = status_filter

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    items = [serialize_attendance_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date_str: str = Path(..., alias="date", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeIn = ...,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attendance record not found")

    current_status = record["status"]
    target_status = body.status if body.status is not None else current_status

    # Rule: setting status ABSENT or LEAVE clears both punch times,
    # so supplying punch_in or punch_out with it is 422
    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Status {target_status} cannot have punch times",
            )
        target_punch_in = None
        target_punch_out = None
    else:
        # Presence status requires a punch_in (supplied or already stored)
        if body.punch_in is not None:
            target_punch_in = epoch_ms_to_datetime(body.punch_in)
        else:
            target_punch_in = record.get("punch_in")

        if target_punch_in is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Presence status requires a punch_in",
            )

        # punch_in must stay on the record's attendance date (R1)
        derived_d = derive_attendance_date(target_punch_in, emp["shift_start"], emp["shift_end"])
        if derived_d != date_str:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="punch_in must stay on the record's attendance date",
            )

        if body.punch_out is not None:
            target_punch_out = epoch_ms_to_datetime(body.punch_out)
        else:
            target_punch_out = record.get("punch_out")

        if target_punch_out is not None:
            diff = (target_punch_out - target_punch_in).total_seconds()
            if diff <= 0:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="punch_out must be strictly after punch_in",
                )
            if diff > 86400:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="punch_out cannot be more than 24 hours after punch_in",
                )

    # Recompute derived fields
    if target_status in ("ABSENT", "LEAVE"):
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_late_minutes = compute_late_minutes(target_punch_in, emp["shift_start"], date_str)
        new_work_hours = compute_work_hours(target_punch_in, target_punch_out)
        new_overtime_minutes = compute_overtime_minutes(
            target_punch_out, emp["shift_start"], emp["shift_end"], date_str
        )
        new_half_day = compute_half_day(new_work_hours)

    # Calculate actual changes
    changes = {}

    def check_change(field_name, old_val, new_val):
        if old_val != new_val:
            changes[field_name] = {"from": old_val, "to": new_val}

    check_change("status", record.get("status"), target_status)
    check_change("punch_in", record.get("punch_in"), target_punch_in)
    check_change("punch_out", record.get("punch_out"), target_punch_out)
    check_change("work_hours", record.get("work_hours"), new_work_hours)
    check_change("late_minutes", record.get("late_minutes", 0), new_late_minutes)
    check_change("overtime_minutes", record.get("overtime_minutes", 0), new_overtime_minutes)
    check_change("half_day", record.get("half_day", False), new_half_day)

    # A request that changes nothing is 422
    if not changes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Request makes no changes to the attendance record",
        )

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic locking on history length to prevent losing concurrent edits
    cur_history_len = len(record.get("history", []))
    update_doc = {
        "$set": {
            "status": target_status,
            "punch_in": target_punch_in,
            "punch_out": target_punch_out,
            "work_hours": new_work_hours,
            "late_minutes": new_late_minutes,
            "overtime_minutes": new_overtime_minutes,
            "half_day": new_half_day,
        },
        "$push": {"history": history_entry},
    }

    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], f"history.{cur_history_len}": {"$exists": False}},
        update_doc,
        return_document=True,
    )

    if not updated:
        # Loser of concurrent regularization gets 409
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Concurrent modification conflict. Please retry.",
        )

    return serialize_attendance_doc(updated)


# --------------------------------------------------------------------------- #
# Analytics Aggregation Endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    # Calculate working days per R7
    year, mo = map(int, month.split("-"))
    num_days = calendar.monthrange(year, mo)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    joined_on = emp["joined_on"]
    # Start counting working days from max(month_start, joined_on)
    start_date_str = max(month_start, joined_on)
    working_days = 0
    if start_date_str <= month_end:
        cur_d = date.fromisoformat(start_date_str)
        end_d = date.fromisoformat(month_end)
        while cur_d <= end_d:
            if cur_d.weekday() < 5:  # Mon-Fri
                working_days += 1
            cur_d += timedelta(days=1)

    # MongoDB aggregation for attendance summary
    pipe = [
        {"$match": {"emp_code": emp_code, "date": {"$regex": f"^{month}"}}},
        {
            "$addFields": {
                "is_weekend": {
                    "$in": [
                        {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                        [6, 7],
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$eq": ["$is_weekend", False]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]

    res = list(db.attendance_logs.aggregate(pipe))
    if res:
        agg = res[0]
        present_days = float(agg["present_days"])
        leave_days = int(agg["leave_days"])
        late_count = int(agg["late_count"])
        total_late = int(agg["total_late_minutes"])
        total_ot = int(agg["total_overtime_minutes"])
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        att_pct = round_half_up((present_days / working_days) * 100.0, 2)
    else:
        att_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": att_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    year, mo = map(int, month.split("-"))
    num_days = calendar.monthrange(year, mo)[1]
    month_end = f"{month}-{num_days:02d}"

    match_emp = {"joined_on": {"$lte": month_end}}
    if department:
        match_emp["department"] = department

    pipe = [
        {"$match": match_emp},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "emp_codes": {"$push": "$emp_code"},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"dept_emps": "$emp_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$in": ["$emp_code", "$$dept_emps"]},
                                    {"$regexMatch": {"input": "$date", "regex": f"^{month}"}},
                                ]
                            }
                        }
                    },
                    {
                        "$addFields": {
                            "is_weekend": {
                                "$in": [
                                    {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                                    [6, 7],
                                ]
                            }
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$eq": ["$$l.is_weekend", False]},
                                        ]
                                    },
                                    {"$cond": [{"$eq": ["$$l.half_day", True]}, 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "valid_work_hours": {
                    "$filter": {
                        "input": "$logs.work_hours",
                        "as": "wh",
                        "cond": {"$ne": ["$$wh", None]},
                    }
                },
                "late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$gt": ["$$l.late_minutes", 0]}, 1, 0]},
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$ifNull": ["$$l.late_minutes", 0]},
                        }
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$eq": ["$$l.status", "LEAVE"]}, 1, 0]},
                        }
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$eq": ["$$l.status", "ON_DUTY"]}, 1, 0]},
                        }
                    }
                },
            }
        },
        {"$sort": {"department": ASCENDING}},
    ]

    raw_items = list(db.employees.aggregate(pipe))
    items = []
    for r in raw_items:
        wh_list = r.get("valid_work_hours", [])
        if wh_list:
            avg_wh = round_half_up(sum(wh_list) / len(wh_list), 2)
        else:
            avg_wh = None

        items.append(
            {
                "department": r["department"],
                "headcount": r["headcount"],
                "present_days": float(r["present_days"]),
                "avg_work_hours": avg_wh,
                "late_count": int(r["late_count"]),
                "total_late_minutes": int(r["total_late_minutes"]),
                "leave_count": int(r["leave_count"]),
                "on_duty_count": int(r["on_duty_count"]),
            }
        )

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    pipe = [
        {"$match": {"date": {"$regex": f"^{month}"}, "late_minutes": {"$gt": 0}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipe.append({"$match": {"emp.department": department}})

    pipe.extend(
        [
            {
                "$project": {
                    "_id": 0,
                    "emp_code": "$_id",
                    "name": "$emp.name",
                    "department": "$emp.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
            {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}},
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": DESCENDING},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}},
        ]
    )

    items = list(db.attendance_logs.aggregate(pipe))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    if to_date < from_date:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="'to' date cannot be earlier than 'from' date",
        )

    from_dt = date.fromisoformat(from_date)
    to_dt = date.fromisoformat(to_date)
    days_range = (to_dt - from_dt).days + 1
    if days_range > 92:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="date range cannot exceed 92 days",
        )

    # 404 if department has no employees
    dept_emps = list(
        db.employees.find(
            {"department": department},
            {"_id": 0, "emp_code": 1, "joined_on": 1},
        )
    )
    if not dept_emps:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Department not found or has no employees",
        )

    emp_codes = [e["emp_code"] for e in dept_emps]
    joined_dates = [e["joined_on"] for e in dept_emps]

    from_dt_mongo = datetime.fromisoformat(from_date)
    to_dt_mongo = datetime.fromisoformat(to_date)

    pipe = [
        {
            "$match": {
                "emp_code": {"$in": emp_codes},
                "date": {"$gte": from_date, "$lte": to_date},
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            }
        },
        {
            "$project": {
                "date_str": "$_id",
                "d": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "d",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt_mongo, to_dt_mongo + timedelta(days=1)],
                },
            }
        },
        {
            "$addFields": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$d"}},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "dayOfWeek": {"$isoDayOfWeek": "$d"},
            }
        },
        {
            "$addFields": {
                "is_working_day": {"$not": {"$in": ["$dayOfWeek", [6, 7]]}},
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": joined_dates,
                            "as": "jd",
                            "cond": {"$lte": ["$$jd", "$date"]},
                        }
                    }
                },
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]},
                        None,
                    ]
                }
            }
        },
        {"$sort": {"date": ASCENDING}},
        {
            "$setWindowFields": {
                "sortBy": {"date": ASCENDING},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, "current"]},
                    }
                },
            }
        },
        {
            "$addFields": {
                "moving_avg_7d": {
                    "$cond": [
                        {"$eq": ["$moving_avg_7d", None]},
                        None,
                        {"$round": ["$moving_avg_7d", 4]},
                    ]
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
    ]

    items = list(db.attendance_logs.aggregate(pipe))
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(
        ...,
        pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$",
    ),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: Optional[int] = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status_filter: Optional[str] = Query(None, alias="status", pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="date_from > date_to",
            )
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status_filter:
            q["status"] = status_filter

        skip = (page - 1) * page_size
        cursor = (
            db.attendance_logs.find(q)
            .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
            .skip(skip)
            .limit(page_size)
        )
        explain_res = cursor.explain()
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_res,
        }

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="emp_code and month are required",
            )
        pipe = [
            {"$match": {"emp_code": emp_code, "date": {"$regex": f"^{month}"}}},
            {
                "$group": {
                    "_id": None,
                    "total_late_minutes": {"$sum": "$late_minutes"},
                }
            },
        ]
        explain_res = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_res,
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="month is required",
            )
        year, mo = map(int, month.split("-"))
        num_days = calendar.monthrange(year, mo)[1]
        month_end = f"{month}-{num_days:02d}"

        match_emp = {"joined_on": {"$lte": month_end}}
        if department:
            match_emp["department"] = department
        pipe = [{"$match": match_emp}, {"$group": {"_id": "$department", "headcount": {"$sum": 1}}}]
        explain_res = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipe, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": explain_res,
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="month is required",
            )
        pipe = [
            {"$match": {"date": {"$regex": f"^{month}"}, "late_minutes": {"$gt": 0}}},
            {"$group": {"_id": "$emp_code", "total_late": {"$sum": "$late_minutes"}}},
        ]
        explain_res = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_res,
        }

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="department, from, and to are required",
            )
        pipe = [
            {
                "$match": {
                    "date": {"$gte": from_date, "$lte": to_date},
                }
            },
            {"$group": {"_id": "$date", "cnt": {"$sum": 1}}},
        ]
        explain_res = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_res,
        }

    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unsupported endpoint")
